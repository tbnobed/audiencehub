"""Server-only regression definitions. No real provider connections."""
import base64
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from app.auth import local
from app.auth.passwords import token_hash
from app.auth.recovery import replace_codes, verify
from app.activations.security import encrypt, decrypt
from app.operations import email, email_api, api
from test_m8_operations_pg import db, local_client, PASSWORD


def configure(db, user):
    body = email_api.Relay(enabled=True, from_email="security@example.org",
                           from_name="Kinship", public_url="https://kinship.example.org",
                           api_key="SG.server-only-fake-key")
    email_api.save_relay(body, user, db)


def test_write_only_relay_configuration(local_client, db):
    _, user = local_client
    configure(db, user)
    result = email_api.get_relay(user, db)
    assert result["key_configured"] and "api_key" not in result
    value = db.scalar(text("SELECT api_key FROM account_email_settings WHERE id=1"))
    assert b"SG.server-only-fake-key" not in bytes(value)
    assert decrypt(value)["key"] == "SG.server-only-fake-key"
    email_api.save_relay(email_api.Relay(enabled=True, from_email="security@example.org",
        public_url="https://kinship.example.org"), user, db)
    assert decrypt(db.scalar(text("SELECT api_key FROM account_email_settings WHERE id=1")))["key"] == "SG.server-only-fake-key"


@pytest.mark.parametrize("url", ["http://kinship.example.org", "https://u:p@kinship.example.org",
                                "https://kinship.example.org/#fragment", "https://kinship.example.org/?token=x"])
def test_account_link_url_must_be_trusted_https(url):
    with pytest.raises(ValueError):
        email.public_url(url)


def test_invitation_is_encrypted_and_queued_atomically(local_client, db):
    _, user = local_client
    configure(db, user)
    issued = api.invite(api.Invite(email="new@example.org", name="New User"), user, db)
    assert issued["email_status"] == "queued" and "token" not in issued
    row = db.execute(text("SELECT * FROM account_email_outbox WHERE id=:id"),
                     {"id": issued["message_id"]}).mappings().one()
    content = decrypt(row["payload"])["content"]
    token = content.split("/login?token=")[1].splitlines()[0]
    assert token_hash(token) == row["token_hash"]
    assert token.encode() not in bytes(row["payload"])
    job_payload = db.scalar(text("SELECT payload FROM jobs WHERE id=:id"), {"id": row["job_id"]})
    assert job_payload == {"message_id": issued["message_id"]}
    assert token not in str(email_api.history(user, db))


def test_provider_acceptance_clears_payload_and_is_idempotent(local_client, db, monkeypatch):
    _, user = local_client
    configure(db, user)
    key = email.queue_message(db, recipient="admin@example.org", subject="test", content="test", purpose="test")
    db.commit()
    calls = []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(status_code=202)
    monkeypatch.setattr(email, "engine", db.get_bind())
    monkeypatch.setattr(email.httpx, "Client", Client)
    email.deliver(key)
    email.deliver(key)
    assert len(calls) == 1 and calls[0][0] == "https://api.sendgrid.com/v3/mail/send"
    assert calls[0][1]["json"]["tracking_settings"]["click_tracking"]["enable"] is False
    row = db.execute(text("SELECT payload,accepted_at FROM account_email_outbox WHERE id=:id"), {"id": key}).one()
    assert row.payload is None and row.accepted_at


def test_superseded_invite_is_not_sent(local_client, db, monkeypatch):
    _, user = local_client
    configure(db, user)
    issued = local.issue_token(db, user.id)
    queued = email.account_message(db, user, issued, "reset")
    local.issue_token(db, user.id)  # Invalidates the queued token.
    db.commit()
    monkeypatch.setattr(email, "engine", db.get_bind())
    monkeypatch.setattr(email.httpx, "Client", lambda **kwargs: pytest.fail("must not contact provider"))
    email.deliver(queued["message_id"])
    row = db.execute(text("SELECT payload,cancelled_at FROM account_email_outbox WHERE id=:id"),
                     {"id": queued["message_id"]}).one()
    assert row.payload is None and row.cancelled_at


def test_totp_qr_is_local_and_confirmation_returns_hashed_recovery_codes(local_client, db, monkeypatch):
    _, user = local_client
    result = local.setup_totp(local.PasswordOnly(password=PASSWORD), user, db)
    assert result["qr_data_uri"].startswith("data:image/svg+xml;base64,")
    assert b"<svg" in base64.b64decode(result["qr_data_uri"].split(",", 1)[1])
    assert "algorithm=SHA1&digits=6&period=30" in result["uri"]
    monkeypatch.setattr(local, "totp_counter", lambda *args, **kwargs: 1)
    request = SimpleNamespace(session={})
    enabled = local.confirm_totp(local.Code(code="123456"), request, user, db)
    assert enabled["csrf_token"] == request.session["csrf"]
    codes = enabled["recovery_codes"]
    assert len(codes) == len(set(codes)) == 10
    hashes = db.scalars(text("SELECT code_hash FROM local_recovery_codes WHERE user_id=:id"), {"id": user.id}).all()
    assert not set(hashes).intersection(codes)
    row = local.credentials(db, user.id)
    assert verify(db, user.id, row, codes[0])
    assert not verify(db, user.id, row, codes[0])
    db.commit()
    assert local.status(db, user)["recovery_codes_remaining"] == 9


def test_recovery_replacement_and_explicit_admin_reset(local_client, db):
    _, user = local_client
    db.execute(text("UPDATE local_credentials SET totp_secret=:secret WHERE user_id=:id"),
               {"id": user.id, "secret": encrypt({"secret": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"})})
    old = replace_codes(db, user.id)
    new = replace_codes(db, user.id)
    row = local.credentials(db, user.id)
    assert not verify(db, user.id, row, old[0])
    assert verify(db, user.id, row, new[0])
    api.reset(user.id, api.Reset(reset_totp=True), user, db)
    assert not local.status(db, user)["mfa_enabled"]
    assert local.status(db, user)["recovery_codes_remaining"] == 0
