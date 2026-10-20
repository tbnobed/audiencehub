"""Regression definitions only: execute on the server, not in Replit."""
import copy
import pytest
from app.meta_capture import DEFAULT_CONFIG, config_valid, lead_fields
from app.meta_privacy import safe_rule, safe_mapping, safe_capi
from app.ingestion.validation import validate
from app.ingestion.service import identities
from app.identity.resolver import _record_identifiers


def giving_rule():
    return {"version": 1, "root": {"op": "and", "rules": [
        {"type": "trait", "field": "lifetime_amount", "operator": "gte", "value": 50},
        {"type": "consent", "channel": "ads_personalization", "status": "opted_in"}]}}


def test_meta_policy_accepts_giving_and_blocks_viewing_proxies():
    safe_rule(None, giving_rule())
    for rule in [
        {"type": "attribute", "field": "vod_viewed", "operator": "is_true"},
        {"type": "event", "name": "VOD Viewed", "aggregate": "count"},
        {"type": "profile", "field": "last_seen_at", "operator": "exists"},
        {"type": "attribute", "field": "tbn_plus_plan", "operator": "exists"},
        {"type": "enrichment", "source": "model_scores", "field": "viewing_score"},
        {"type": "source", "source_key": "vod_viewed_events"},
        {"type": "gift", "where": {"vod_titles.name": {"operator": "eq", "value": "program"}}},
    ]:
        with pytest.raises(ValueError):
            safe_rule(None, {"version": 1, "root": rule})


def test_meta_policy_checks_nested_audience_rules_and_cycles():
    class DB:
        def scalar(self, sql, params):
            if params["id"] == 1:
                return {"root": {"type": "attribute", "field": "vod_title", "operator": "exists"}}
            return {"root": {"type": "segment", "segment_id": 2}}
    for identifier in (1, 2):
        with pytest.raises(ValueError):
            safe_rule(DB(), {"root": {"type": "segment", "segment_id": identifier}})


def test_meta_mapping_rejects_constants_enrichment_and_video_aliases():
    safe_mapping({"email": {"kind": "profile", "key": "email"}})
    for mapping in [
        {"email": {"kind": "constant", "value": "program name"}},
        {"video_title": {"kind": "profile", "key": "email"}},
        {"email": {"kind": "attribute", "key": "vod_name"}},
    ]:
        with pytest.raises(ValueError):
            safe_mapping(mapping)


def test_capi_allowlist_blocks_urls_content_and_plaintext():
    payload = {"data": [{"event_name": "Donate", "event_time": 1800000000,
                         "action_source": "website", "user_data": {"em": ["a" * 64]},
                         "custom_data": {"value": 50, "currency": "USD"}}]}
    safe_capi(payload)
    for key, value in [
        ("event_source_url", "https://example.org/watch/private-title"),
        ("event_name", "VideoView"),
        ("custom_data", {"content_name": "private-title"}),
        ("user_data", {"em": ["not-a-hash"]}),
        ("custom_data", {"value": float("nan")}),
    ]:
        invalid = copy.deepcopy(payload)
        invalid["data"][0][key] = value
        with pytest.raises(ValueError):
            safe_capi(invalid)


def test_generic_webhooks_cannot_bypass_meta_policy():
    from app.activations.security import target, DeliveryError
    with pytest.raises(DeliveryError, match="Meta"):
        target("https://graph.facebook.com/v25.0/123/events")


def test_capture_config_binds_checkbox_wording_to_exact_form():
    config = {**DEFAULT_CONFIG, "page_id": "123", "app_id": "456",
              "consent_field": "email_permission", "consent_text": "Email me updates."}
    with pytest.raises(ValueError, match="form ID"):
        config_valid(config)
    config["consent_form_id"] = "789"
    assert config_valid(config)["consent_form_id"] == "789"
    invalid = {**config, "field_mapping": {"video_title": ["vod_title"]}}
    with pytest.raises(ValueError):
        config_valid(invalid)


def test_disclaimer_checkbox_is_not_lost_in_field_data_mapping():
    data = {"field_data": [{"name": "email", "values": ["person@example.org"]}],
            "custom_disclaimer_responses": [{"checkbox_key": "email_permission", "is_checked": True}]}
    assert lead_fields(data) == {"email": "person@example.org", "email_permission": "True"}


def test_attribution_never_becomes_identity_evidence():
    class Source:
        key, id = "browser", 12
    message = validate({"type": "page", "anonymousId": "anonymous-session",
                        "fbclid": "opaque-click", "_fbp": "fb.1.123.456", "_fbc": "fb.1.123.click"})
    assert message["attribution"]["fbclid"] == "opaque-click"
    assert not {"fbclid", "_fbp", "_fbc"} & {kind for kind, _ in identities(Source(), message)}
    record = {"external_id": "row", "attributes": {
        "meta_app_user_id": "123:456", "meta_psid": "123:789", "fbclid": "opaque-click"}}
    assert not {"fbclid", "meta_app_user_id", "meta_psid"} & {
        kind for kind, _ in _record_identifiers(record, "browser")}


def test_conversion_contact_is_in_suppression_scope_before_capture():
    class Source:
        key, id = "browser", 12
    message = validate({"type": "track", "event": "Donation", "anonymousId": "anonymous-session",
                        "properties": {"email": "person@example.org"}})
    assert ("email", "person@example.org") in identities(Source(), message)
    malformed = validate({"type": "track", "event": "Donation", "anonymousId": "anonymous-session",
                          "properties": {"email": 123}})
    identities(Source(), malformed)  # Non-string arbitrary event properties are not contact keys.


def test_facebook_login_verifies_app_subject_and_expiry(monkeypatch):
    import app.meta_api as api
    class FakeMeta:
        app = "123"
        subject = "456"
        expires = 0
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def request(self, method, path, *args, **kwargs):
            if path == "/debug_token":
                return {"data": {"is_valid": True, "app_id": self.app, "type": "USER",
                                 "user_id": "456", "expires_at": self.expires}}
            return {"id": self.subject}
    monkeypatch.setattr(api, "Meta", FakeMeta)
    config, credentials = {"app_id": "123"}, {"app_secret": "non-secret-test-value"}
    assert api.verified_facebook_profile(config, credentials, "test-token")["id"] == "456"
    for key, value in [("app", "999"), ("subject", "789"), ("expires", 1)]:
        with monkeypatch.context() as patch:
            patch.setattr(FakeMeta, key, value)
            with pytest.raises(ValueError):
                api.verified_facebook_profile(config, credentials, "test-token")
