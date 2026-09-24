from app.profiles.api import _json_safe, router


def test_viewer_masking_recurses_across_profile_payload():
    result = _json_safe({
        "email": "person@example.org",
        "identifiers": [{"type": "phone", "value": "+1 214 748 3647"}],
        "attributes": {"note": "Contact person@example.org at 214-748-3647"},
    }, mask_pii=True)

    assert result["email"] == "p***@example.org"
    assert result["identifiers"][0]["value"] == "***-***-3647"
    assert "person@example.org" not in result["attributes"]["note"]
    assert "214-748-3647" not in result["attributes"]["note"]


def test_non_viewer_profile_payload_keeps_identifiers_unmasked():
    result = _json_safe({"email": "person@example.org", "phone": "214-748-3647"})
    assert result == {"email": "person@example.org", "phone": "214-748-3647"}


def test_profile_and_data_health_contract_routes_registered():
    routes = {(tuple(sorted(route.methods or [])), route.path) for route in router.routes}
    assert (("GET",), "/api/profiles") in routes
    assert (("GET",), "/api/profiles/{profile_id}") in routes
    assert (("GET",), "/api/data-health") in routes
    assert (("POST",), "/api/data-health/blocklist/{blocklist_id}/approve") in routes
    assert (("POST",), "/api/data-health/blocklist/{blocklist_id}/unblock") in routes