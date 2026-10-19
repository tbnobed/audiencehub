"""Local provider: credentials are separate from provider-neutral users."""
import secrets
import base64
import io
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app.auth.deps import check_csrf, current_user
from app.auth.passwords import check_password, hash_password, new_totp_secret, token_hash, totp_counter, validate_password
from app.activations.security import encrypt, decrypt
from app.config import get_settings
from app.db import session_scope
from app.models import AuditLog, User, now
from app.auth.recovery import replace_codes, verify as verify_second_factor

router = APIRouter()


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)
    totp: str = Field(default="", max_length=20)


class Accept(BaseModel):
    token: str = Field(min_length=32, max_length=256)
    password: str = Field(max_length=1024)


class Password(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)
    totp: str = Field(default="", max_length=20)


class PasswordOnly(BaseModel):
    password: str = Field(max_length=1024)


class Code(BaseModel):
    code: str = Field(max_length=20)


class Disable(PasswordOnly, Code):
    pass


def audit(db, action, user_id=None, details=None):
    db.add(AuditLog(user_id=user_id, actor_type="user" if user_id else "anonymous",
                    action=action, entity_type="user",
                    entity_id=str(user_id) if user_id else None, details=details or {}))


def credentials(db, user_id):
    db.execute(text("INSERT INTO local_credentials(user_id) VALUES(:id) ON CONFLICT DO NOTHING"), {"id": user_id})
    return db.execute(text("SELECT * FROM local_credentials WHERE user_id=:id FOR UPDATE"), {"id": user_id}).mappings().one()


def revoke(db, user_id):
    db.execute(text("DELETE FROM local_sessions WHERE user_id=:id"), {"id": user_id})


def local_only():
    if get_settings().auth_mode != "local":
        raise HTTPException(404, "Local accounts are disabled")


def status(db, user):
    row = db.execute(text("SELECT totp_secret IS NOT NULL FROM local_credentials WHERE user_id=:id"), {"id": user.id}).scalar()
    required = user.role == "admin" and bool(db.scalar(text("SELECT require_admin_totp FROM operations_policy WHERE id=1")))
    remaining = db.scalar(text("SELECT count(*) FROM local_recovery_codes WHERE user_id=:id"), {"id": user.id})
    return {"mfa_enabled": bool(row), "mfa_required": required, "recovery_codes_remaining": remaining}


def establish(db, request, user, verified=False):
    sid = secrets.token_urlsafe(48)
    db.execute(text("INSERT INTO local_sessions(token_hash,user_id,mfa_verified) VALUES(:hash,:id,:mfa)"),
               {"hash": token_hash(sid), "id": user.id, "mfa": verified})
    user.last_login_at = now()
    audit(db, "auth.login", user.id, {"provider": "local", "mfa": verified})
    db.commit()
    request.session.clear()
    request.session.update(uid=user.id, sid=sid, csrf=secrets.token_urlsafe(32))


def issue_token(db, user_id, purpose="reset"):
    token = secrets.token_urlsafe(48)
    expiry = now() + timedelta(hours=48 if purpose == "invite" else 1)
    db.execute(text("DELETE FROM local_account_tokens WHERE user_id=:id"), {"id": user_id})
    db.execute(text("""INSERT INTO local_account_tokens(token_hash,user_id,purpose,expires_at)
        VALUES(:hash,:id,:purpose,:expiry)"""),
        {"hash": token_hash(token), "id": user_id, "purpose": purpose, "expiry": expiry})
    return {"user_id": user_id, "token": token, "expires_at": expiry}


def failed(db, user_id, limit_key, reason="invalid_credentials"):
    if user_id:
        row = credentials(db, user_id)
        count = row["failures"] + 1
        db.execute(text("""UPDATE local_credentials SET failures=:n,
            locked_until=CASE WHEN :n>=10 THEN now()+interval '15 minutes' ELSE NULL END WHERE user_id=:id"""),
            {"n": count, "id": user_id})
        if count == 10:
            audit(db, "auth.lockout", user_id, {"cooldown_minutes": 15})
    db.execute(text("""UPDATE local_login_limits SET failures=failures+1,
        locked_until=CASE WHEN failures+1>=100 THEN now()+interval '15 minutes' ELSE NULL END WHERE key_hash=:key"""),
        {"key": limit_key})
    audit(db, "auth.login_failed", user_id, {"reason": reason})
    db.commit()
    raise HTTPException(401, "Sign-in failed. Check credentials or try again after the cooldown.")


@router.get("/auth/local/session")
def session(request: Request):
    local_only()
    request.session.setdefault("csrf", secrets.token_urlsafe(32))
    return {"csrf_token": request.session["csrf"], "mode": "local"}


@router.post("/auth/local/login")
def login(body: Login, request: Request, db: Session = Depends(session_scope)):
    local_only()
    check_csrf(request)
    limit_key = token_hash("login:" + (request.client.host if request.client else "unknown"))
    db.execute(text("INSERT INTO local_login_limits(key_hash) VALUES(:key) ON CONFLICT DO NOTHING"), {"key": limit_key})
    throttle = db.execute(text("SELECT * FROM local_login_limits WHERE key_hash=:key FOR UPDATE"), {"key": limit_key}).mappings().one()
    if throttle["locked_until"] and throttle["locked_until"] > now():
        audit(db, "auth.login_failed", details={"reason": "address_cooldown"})
        db.commit()
        raise HTTPException(429, "Sign-in cooldown. Try again in 15 minutes.")
    if (now()-throttle["window_started"]).total_seconds() >= 900:
        db.execute(text("UPDATE local_login_limits SET failures=0,locked_until=NULL,window_started=now() WHERE key_hash=:key"), {"key": limit_key})
    user = db.scalar(select(User).where(text("lower(users.email)=:email")).params(email=body.email.strip().lower()))
    row = credentials(db, user.id) if user else None
    if row and row["locked_until"] and row["locked_until"] > now():
        audit(db, "auth.login_failed", user.id, {"reason": "account_cooldown"})
        db.commit()
        raise HTTPException(401, "Sign-in failed. Check credentials or try again after the cooldown.")
    if row and row["locked_until"]:
        db.execute(text("UPDATE local_credentials SET failures=0,locked_until=NULL WHERE user_id=:id"), {"id": user.id})
    # Perform scrypt even for unknown/invited users to reduce account enumeration.
    encoded = row["password_hash"] if row and row["password_hash"] else "scrypt$32768$8$1$" + "00"*16 + "$" + "00"*64
    valid = check_password(body.password, encoded)
    if not user or not user.is_active or not valid:
        failed(db, user.id if user else None, limit_key)
    verified = False
    if row["totp_secret"]:
        if not verify_second_factor(db, user.id, row, body.totp):
            failed(db, user.id, limit_key, "invalid_second_factor")
        if len(body.totp) != 6:
            audit(db, "auth.recovery_code_used", user.id)
        verified = True
    db.execute(text("UPDATE local_credentials SET failures=0,locked_until=NULL WHERE user_id=:id"), {"id": user.id})
    db.execute(text("UPDATE local_login_limits SET failures=0,locked_until=NULL WHERE key_hash=:key"), {"key": limit_key})
    establish(db, request, user, verified)
    return {"ok": True}


@router.post("/auth/local/accept")
def accept(body: Accept, request: Request, db: Session = Depends(session_scope)):
    local_only()
    check_csrf(request)
    try:
        validate_password(body.password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    token = db.execute(text("SELECT * FROM local_account_tokens WHERE token_hash=:hash AND expires_at>now() FOR UPDATE"),
                       {"hash": token_hash(body.token)}).mappings().first()
    if not token:
        raise HTTPException(400, "Invalid or expired account link")
    user = db.get(User, token["user_id"])
    if not user or not user.is_active:
        raise HTTPException(400, "Account unavailable")
    row = credentials(db, user.id)
    db.execute(text("""UPDATE local_credentials SET password_hash=:hash,password_changed_at=now(),
        failures=0,locked_until=NULL WHERE user_id=:id"""), {"hash": hash_password(body.password), "id": user.id})
    db.execute(text("DELETE FROM local_account_tokens WHERE user_id=:id"), {"id": user.id})
    revoke(db, user.id)
    audit(db, "auth.password_reset", user.id)
    # Never bypass an enrolled second factor with an invite/reset token.
    db.commit()
    if row["totp_secret"]:
        request.session.clear()
        return {"ok": True, "login_required": True}
    establish(db, request, user)
    return {"ok": True}


@router.get("/api/account/security")
def security(user: User = Depends(current_user), db: Session = Depends(session_scope)):
    return status(db, user)


def reauthenticate(db, user, password, code=None):
    row = credentials(db, user.id)
    if row["locked_until"] and row["locked_until"] > now():
        raise HTTPException(429, "Account locked; try after cooldown")
    if row["locked_until"]:
        db.execute(text("UPDATE local_credentials SET failures=0,locked_until=NULL WHERE user_id=:id"), {"id": user.id})
        row = credentials(db, user.id)
    valid = check_password(password, row["password_hash"])
    factor_valid = True
    if code is not None and row["totp_secret"]:
        # Do not consume a recovery code unless the password was valid.
        factor_valid = valid and verify_second_factor(db, user.id, row, code)
        valid = valid and factor_valid
    if not valid:
        db.execute(text("""UPDATE local_credentials SET failures=failures+1,
            locked_until=CASE WHEN failures+1>=10 THEN now()+interval '15 minutes' ELSE NULL END WHERE user_id=:id"""), {"id": user.id})
        audit(db, "auth.reauthentication_failed", user.id)
        if row["failures"] == 9:
            audit(db, "auth.lockout", user.id)
        db.commit()
        raise HTTPException(403, "Current password or authenticator code is incorrect")
    if code and row["totp_secret"] and factor_valid and len(code) != 6:
        audit(db, "auth.recovery_code_used", user.id)
    return row


@router.post("/api/account/password")
def change_password(body: Password, request: Request, user: User = Depends(current_user), db: Session = Depends(session_scope)):
    reauthenticate(db, user, body.current_password, body.totp)
    try:
        validate_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    db.execute(text("UPDATE local_credentials SET password_hash=:hash,password_changed_at=now(),failures=0 WHERE user_id=:id"),
               {"hash": hash_password(body.new_password), "id": user.id})
    revoke(db, user.id)
    db.execute(text("DELETE FROM local_account_tokens WHERE user_id=:id"), {"id": user.id})
    audit(db, "auth.password_changed", user.id)
    db.commit()
    request.session.clear()
    return {"ok": True}


@router.post("/api/account/totp/setup")
def setup_totp(body: PasswordOnly, user: User = Depends(current_user), db: Session = Depends(session_scope)):
    row = reauthenticate(db, user, body.password)
    if row["totp_secret"]:
        raise HTTPException(409, "Disable the existing authenticator before enrolling a replacement")
    secret = new_totp_secret()
    db.execute(text("UPDATE local_credentials SET pending_totp=:secret,pending_totp_until=now()+interval '10 minutes' WHERE user_id=:id"),
               {"id": user.id, "secret": encrypt({"secret": secret})})
    audit(db, "auth.totp_setup", user.id)
    db.commit()
    uri = f"otpauth://totp/Kinship:{quote(user.email, safe='')}?secret={secret}&issuer=Kinship&algorithm=SHA1&digits=6&period=30"
    # Generated locally: never send the shared secret to an external QR service.
    import segno
    output = io.BytesIO()
    segno.make(uri, micro=False).save(output, kind="svg", scale=5, border=4)
    return {"secret": secret, "uri": uri,
            "qr_data_uri": "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode(),
            "expires_at": (now() + timedelta(minutes=10)).isoformat()}


@router.post("/api/account/totp/confirm")
def confirm_totp(body: Code, request: Request, user: User = Depends(current_user), db: Session = Depends(session_scope)):
    row = credentials(db, user.id)
    if row["locked_until"] and row["locked_until"] > now():
        raise HTTPException(429, "Account locked; wait 15 minutes or ask an administrator to unlock")
    if row["locked_until"]:
        db.execute(text("UPDATE local_credentials SET failures=0,locked_until=NULL WHERE user_id=:id"), {"id": user.id})
        row = credentials(db, user.id)
    if not row["pending_totp"] or row["pending_totp_until"] < now():
        raise HTTPException(400, "Authenticator enrollment expired")
    counter = totp_counter(decrypt(row["pending_totp"])["secret"], body.code)
    if counter is None:
        db.execute(text("""UPDATE local_credentials SET failures=failures+1,
            locked_until=CASE WHEN failures+1>=10 THEN now()+interval '15 minutes' ELSE NULL END WHERE user_id=:id"""), {"id": user.id})
        audit(db, "auth.totp_enrollment_failed", user.id)
        if row["failures"] == 9:
            audit(db, "auth.lockout", user.id)
        db.commit()
        raise HTTPException(422, "Invalid authenticator code")
    db.execute(text("""UPDATE local_credentials SET totp_secret=pending_totp,pending_totp=NULL,
        pending_totp_until=NULL,totp_counter=:counter,failures=0,locked_until=NULL WHERE user_id=:id"""), {"id": user.id, "counter": counter})
    revoke(db, user.id)
    codes = replace_codes(db, user.id)
    audit(db, "auth.totp_enabled", user.id)
    establish(db, request, user, True)
    return {"ok": True, "recovery_codes": codes, "csrf_token": request.session["csrf"]}


@router.post("/api/account/totp/recovery")
def regenerate_recovery(body: Disable, user: User = Depends(current_user), db: Session = Depends(session_scope)):
    row = reauthenticate(db, user, body.password, body.code)
    if not row["totp_secret"]:
        raise HTTPException(409, "Enable an authenticator first")
    codes = replace_codes(db, user.id)
    audit(db, "auth.recovery_codes_regenerated", user.id)
    db.commit()
    return {"recovery_codes": codes}


@router.post("/api/account/totp/disable")
def disable_totp(body: Disable, request: Request, user: User = Depends(current_user), db: Session = Depends(session_scope)):
    reauthenticate(db, user, body.password, body.code)
    if status(db, user)["mfa_required"]:
        raise HTTPException(409, "Authenticator is required for administrators")
    db.execute(text("UPDATE local_credentials SET totp_secret=NULL,pending_totp=NULL,totp_counter=-1 WHERE user_id=:id"), {"id": user.id})
    db.execute(text("DELETE FROM local_recovery_codes WHERE user_id=:id"), {"id": user.id})
    revoke(db, user.id)
    audit(db, "auth.totp_disabled", user.id)
    establish(db, request, user)
    return {"ok": True}
