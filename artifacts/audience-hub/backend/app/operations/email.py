"""SendGrid relay via its public HTTPS API. No platform connector or SDK."""
from urllib.parse import urlsplit
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
import httpx
from app.activations.security import encrypt, decrypt
from app.auth.passwords import token_hash
from app.db import engine
from app.jobs.queue import enqueue
from app.models import AuditLog, now


def public_url(value):
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
        raise ValueError("Public application URL must be HTTPS, without credentials, query or fragment")
    return value


def settings(db):
    return db.execute(text("SELECT * FROM account_email_settings WHERE id=1")).mappings().one()


def queue_message(db, *, recipient, subject, content, purpose, user_id=None,
                  account_token=None, expires_at=None):
    from datetime import timedelta
    row = settings(db)
    if not row["enabled"] or not row["api_key"]:
        raise HTTPException(409, "Configure and enable the SendGrid email relay first")
    message = {"recipient": recipient, "subject": subject, "content": content,
               "from_email": row["from_email"], "from_name": row["from_name"]}
    key = db.scalar(text("""INSERT INTO account_email_outbox
        (user_id,purpose,payload,token_hash,expires_at)
        VALUES(:user,:purpose,:payload,:hash,:expires) RETURNING id"""),
        {"user": user_id, "purpose": purpose, "payload": encrypt(message),
         "hash": token_hash(account_token) if account_token else None,
         "expires": expires_at or now() + timedelta(hours=1)})
    job = enqueue(db, "account.email", {"message_id": key}, max_attempts=5)
    db.execute(text("UPDATE account_email_outbox SET job_id=:job WHERE id=:id"), {"job": job.id, "id": key})
    return key


def account_message(db, user, result, purpose):
    """Queue in the account transaction, never persist a plaintext bearer link."""
    config = settings(db)
    if not config["enabled"]:
        return {**result, "email_status": "disabled"}
    url = public_url(config["public_url"]) + "/login?token=" + result["token"]
    label = "Set up your Kinship account" if purpose == "invite" else "Reset your Kinship password"
    message_id = queue_message(
        db, recipient=user.email, subject=label,
        content=f"{label}\n\n{url}\n\nThis single-use link expires at {result['expires_at'].isoformat()}."
                "\nAn existing authenticator is still required after resetting your password."
                "\nIf you did not expect this message, contact your Kinship administrator.",
        purpose=purpose, user_id=user.id, account_token=result["token"], expires_at=result["expires_at"])
    return {"user_id": user.id, "expires_at": result["expires_at"],
            "email_status": "queued", "message_id": message_id}


def delivery_failed(db, message_id, message):
    # Only callers' fixed messages/status integers, never provider bodies.
    db.execute(text("UPDATE account_email_outbox SET last_error=:error WHERE id=:id"),
               {"id": message_id, "error": message})
    db.add(AuditLog(actor_type="system", action="email.attempt_failed",
                    entity_type="account_email", entity_id=str(message_id),
                    details={"reason": message}))
    db.commit()
    raise ValueError(message) from None


def deliver(message_id):
    # Hold the outbox row for the bounded HTTP request to serialize retries.
    # SendGrid has no idempotency guarantee: a lost HTTP response may resend the
    # same single-use link, never mint a second token.
    with Session(engine) as db:
        row = db.execute(text("SELECT * FROM account_email_outbox WHERE id=:id FOR UPDATE"),
                         {"id": message_id}).mappings().first()
        if not row or row["accepted_at"] or row["cancelled_at"]:
            return
        valid = row["payload"] and row["expires_at"] > now()
        if row["token_hash"]:
            valid = valid and db.scalar(text("""SELECT EXISTS(
                SELECT 1 FROM local_account_tokens t JOIN users u ON u.id=t.user_id
                WHERE t.token_hash=:hash AND t.user_id=:user AND t.expires_at>now() AND u.is_active)"""),
                {"hash": row["token_hash"], "user": row["user_id"]})
        if not valid:
            db.execute(text("UPDATE account_email_outbox SET payload=NULL,cancelled_at=now() WHERE id=:id"),
                       {"id": message_id})
            db.commit()
            return
        config = settings(db)
        if not config["enabled"] or not config["api_key"]:
            delivery_failed(db, message_id, "SendGrid relay is disabled; enable it before retrying")
        message = decrypt(row["payload"])
        body = {"personalizations": [{"to": [{"email": message["recipient"]}]}],
                "from": {"email": message["from_email"], "name": message["from_name"]},
                "subject": message["subject"],
                "content": [{"type": "text/plain", "value": message["content"]}],
                "tracking_settings": {"click_tracking": {"enable": False, "enable_text": False},
                                      "open_tracking": {"enable": False}}}
        try:
            with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
                response = client.post("https://api.sendgrid.com/v3/mail/send", json=body,
                    headers={"Authorization": "Bearer " + decrypt(config["api_key"])["key"]})
        except httpx.HTTPError:
            delivery_failed(db, message_id, "SendGrid request failed; delivery will retry")
        if response.status_code != 202:
            # Never expose provider response bodies, Authorization or email links.
            delivery_failed(db, message_id, f"SendGrid rejected the message (HTTP {response.status_code}); check sender verification and Mail Send permission")
        db.execute(text("UPDATE account_email_outbox SET accepted_at=now(),payload=NULL,last_error=NULL WHERE id=:id"), {"id": message_id})
        db.add(AuditLog(actor_type="system", action="email.accepted", entity_type="account_email",
                        entity_id=str(message_id), details={"purpose": row["purpose"]}))
        db.commit()
