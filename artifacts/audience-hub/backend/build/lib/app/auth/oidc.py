from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.auth.deps import start_session
from app.config import get_settings
from app.db import engine
from app.models import User, now

router = APIRouter()
oauth = OAuth()


def configure_oidc():
    settings = get_settings()
    if settings.auth_mode == "oidc":
        oauth.register(
            name="identity", client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            server_metadata_url=settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration",
            client_kwargs={"scope": "openid profile email"},
            code_challenge_method="S256",
        )


@router.get("/auth/login")
async def login(request: Request):
    settings = get_settings()
    if settings.auth_mode == "dev":
        return RedirectResponse("/")
    return await oauth.identity.authorize_redirect(request, settings.public_base_url.rstrip("/") + "/auth/callback",
                                                    code_challenge_method="S256")


@router.get("/auth/callback")
async def callback(request: Request):
    if get_settings().auth_mode != "oidc":
        raise HTTPException(404)
    token = await oauth.identity.authorize_access_token(request)
    claims = token.get("userinfo")
    if not claims or not claims.get("sub"):
        raise HTTPException(401, detail="Invalid identity token")
    settings = get_settings()
    groups = claims.get(settings.oidc_role_claim) or []
    if isinstance(groups, str):
        groups = [groups]
    role = next((r for r, names in (
        ("admin", settings.oidc_admin_groups), ("analyst", settings.oidc_analyst_groups),
        ("viewer", settings.oidc_viewer_groups))
        if set(groups) & set(names.split(","))), None)
    if not role:
        raise HTTPException(403, detail="Your account is not in an Audience Hub group")
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.subject == claims["sub"]))
        if not user:
            user = User(subject=claims["sub"], email=claims.get("email", ""),
                        name=claims.get("name", ""), role=role)
            db.add(user)
        user.email = claims.get("email", "")
        user.name = claims.get("name", "")
        user.role = role
        user.last_login_at = now()
        db.commit()
        db.refresh(user)
        start_session(request, user)
    return RedirectResponse("/")