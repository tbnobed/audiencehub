"""Code-only regressions. Mock transports never contact HubSpot."""
import hashlib
import hmac
import json
from types import SimpleNamespace

import httpx
import pytest

from app.hubspot_client import HubSpot, missing_scopes, validate_token
from app.hubspot_destination import ensure_list, ensure_property, sync_master_ids
from app.hubspot_source import mapped_contact
from app.source_connectors import SourceError

TOKEN = "not-a-real-private-app-token"


def client(handler, sleep=lambda _: None):
    return HubSpot({"access_token": TOKEN}, transport=httpx.MockTransport(handler), sleep=sleep)


def test_token_inspection_is_post_body_not_url():
    def handle(request):
        assert request.method == "POST"
        assert request.url.path == "/oauth/v2/private-apps/get/access-token-info"
        assert TOKEN not in str(request.url)
        assert json.loads(request.content) == {"tokenKey": TOKEN}
        return httpx.Response(200, json={"hubId": 10, "appId": 20, "scopes": ["crm.objects.contacts.read"]})
    with client(handle) as connection:
        info = connection.info()
    assert info["account"] == {"hub_id": 10, "app_id": 20}
    assert "communication_preferences.read" in info["missing_scopes"]["subscriptions"]
    assert TOKEN not in json.dumps(info)


@pytest.mark.parametrize("credentials", [{}, {"access_token": ""}, {"access_token": "has spaces"},
                                        {"access_token": "ok", "url": "https://evil.example"}])
def test_reject_invalid_token_configuration(credentials):
    with pytest.raises(SourceError):
        validate_token(credentials)


@pytest.mark.parametrize("alternative", ["communication_preferences.read_write",
                                       "communication_preferences.statuses.batch.read"])
def test_subscription_scope_alternatives(alternative):
    assert missing_scopes([alternative])["subscriptions"] == []


def test_vendor_errors_do_not_echo_token_or_body():
    with client(lambda _: httpx.Response(400, json={"message": TOKEN})) as connection:
        with pytest.raises(SourceError) as error:
            connection.info()
    assert TOKEN not in str(error.value)


def test_retry_after_is_respected():
    requests, waits = [], []
    def handle(request):
        requests.append(request)
        return (httpx.Response(429, headers={"Retry-After": "2"}) if len(requests) == 1
                else httpx.Response(200, json={"results": []}))
    with client(handle, waits.append) as connection:
        assert connection.request("GET", "/crm/v3/properties/contacts") == {"results": []}
    assert len(requests) == 2
    assert any(wait >= 2 for wait in waits)


def test_keyset_contact_search_and_batch_read():
    def handle(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/search"):
            assert {"propertyName": "hs_object_id", "operator": "GT", "value": "123"} in body["filterGroups"][0]["filters"]
            assert "after" not in body
            return httpx.Response(200, json={"results": [{"id": "202"}, {"id": "201"}]})
        assert body["inputs"] == [{"id": "202"}, {"id": "201"}]
        assert "hs_email_optout" in body["properties"]
        return httpx.Response(200, json={"results": [{"id": "202", "properties": {}}, {"id": "201", "properties": {}}]})
    with client(handle) as connection:
        assert [c["id"] for c in connection.contacts(1, 2, "123", ["firstname"])] == ["201", "202"]


def test_partial_batch_fails_without_accepting_successful_subset():
    with client(lambda _: httpx.Response(207, json={"results": [{"id": "1"}], "errors": [{"message": TOKEN}]})) as connection:
        with pytest.raises(SourceError):
            connection.request("POST", "/crm/v3/objects/contacts/batch/read", body={"inputs": [{"id": "1"}]})


def test_preferences_preserve_subscription_type_and_timestamp():
    status = {"subscriptionId": 17, "status": "UNSUBSCRIBED", "timestamp": "2026-10-18T00:00:00Z"}
    with client(lambda _: httpx.Response(200, json={"status": "COMPLETE", "results": [
        {"subscriberIdString": "Partner@example.org", "statuses": [status]}]})) as connection:
        result = connection.preferences(["partner@example.org"], ["communication_preferences.statuses.batch.read"])
    assert result == {"partner@example.org": [status]}


def test_missing_preferences_do_not_silently_mean_subscribed():
    with client(lambda _: httpx.Response(200, json={"status": "COMPLETE", "results": []})) as connection:
        with pytest.raises(SourceError):
            connection.preferences(["partner@example.org"], ["communication_preferences.statuses.batch.read"])


def test_real_identity_key_cannot_be_remapped():
    row = mapped_contact({"id": "123", "properties": {"email": "partner@example.org", "firstname": "Mary"}},
                         {"columns": {"hs_object_id": "email"}})
    assert row["hubspot_record_id"] == "123"
    assert row["external_id"] == "123"
    assert row["email"] == "partner@example.org"
    assert row["first_name"] == "Mary"


def test_create_master_id_property_only_when_absent():
    requests = []
    def handle(request):
        requests.append(request)
        return (httpx.Response(404) if request.method == "GET" else
                httpx.Response(201, json={"name": "kinship_master_id", "type": "string"}))
    with client(handle) as connection:
        ensure_property(connection)
    assert [r.method for r in requests] == ["GET", "POST"]


def test_master_id_updates_skip_correct_values():
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/batch/read"):
            return httpx.Response(200, json={"results": [
                {"id": "1", "properties": {"kinship_master_id": "kin_correct"}},
                {"id": "2", "properties": {"kinship_master_id": "wrong"}}]})
        assert json.loads(request.content)["inputs"] == [{"id": "2", "properties": {"kinship_master_id": "kin_new"}}]
        return httpx.Response(200, json={"results": [{"id": "2"}]})
    with client(handle) as connection:
        _, updated, unchanged = sync_master_ids(connection, {"1": {"master_id": "kin_correct"}, "2": {"master_id": "kin_new"}})
    assert (updated, unchanged) == (1, 1)
    assert len(requests) == 2


def test_dynamic_list_is_never_overwritten():
    with client(lambda _: httpx.Response(200, json={"list": {
        "listId": "99", "name": "Existing", "processingType": "DYNAMIC", "objectTypeId": "0-1"}})) as connection:
        with pytest.raises(SourceError):
            ensure_list(connection, {"list_id": "99", "list_name": "Existing"})


@pytest.mark.parametrize("kind", ["csv", "webhook", "sftp", "google_customer_match", "meta_custom_audience"])
def test_hubspot_suppression_cannot_be_bypassed_by_no_consent_channel(monkeypatch, kind):
    from app.activations import service
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(pii_hash_pepper="test-pepper"))
    hashed = hmac.new(b"test-pepper", b"partner@example.org", hashlib.sha256).hexdigest()
    class Result:
        def mappings(self):
            return [{"type": "email", "value_hash": hashed, "reason": "hubspot_unsubscribe"}]
    class Database:
        def execute(self, *args, **kwargs):
            return Result()
    snapshot = {"type": kind, "consent": None, "config": {},
                "mapping": {"email": {"kind": "profile", "key": "email"}}}
    assert service.export_suppressed_ids(Database(), [{"id": 42, "email": "partner@example.org", "phone": None}], snapshot) == {42}
