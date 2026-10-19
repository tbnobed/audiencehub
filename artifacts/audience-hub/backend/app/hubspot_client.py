"""Fixed-origin HubSpot API client. Never expose vendor error bodies or tokens."""
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from app.source_connectors import SourceError

DEFAULT_PROPERTIES = ["email", "firstname", "lastname", "phone", "address", "city",
                      "state", "zip", "country", "hs_object_id", "lastmodifieddate", "hs_email_optout"]
SCOPES = {
    "contacts": ["crm.objects.contacts.read"],
    "properties": ["crm.schemas.contacts.read"],
    "subscriptions": ["communication_preferences.read"],
    "engagement": ["content"],
    "master_id": ["crm.objects.contacts.read", "crm.objects.contacts.write",
                  "crm.schemas.contacts.read", "crm.schemas.contacts.write"],
    "lists": ["crm.lists.read", "crm.lists.write"],
}


def validate_token(credentials):
    if (not isinstance(credentials, dict) or set(credentials) != {"access_token"}
            or not isinstance(credentials.get("access_token"), str)
            or not credentials["access_token"].strip() or len(credentials["access_token"]) > 4096
            or any(c.isspace() for c in credentials["access_token"])):
        raise SourceError("Enter a HubSpot private app access token.")


def missing_scopes(scopes):
    granted = set(scopes)
    result = {feature: [scope for scope in required if scope not in granted]
              for feature, required in SCOPES.items()}
    if granted & {"communication_preferences.read_write", "communication_preferences.statuses.batch.read"}:
        result["subscriptions"] = []
    return result


def validate_source(config):
    from app.source_connectors import validate_schedule
    if not isinstance(config, dict) or set(config) - {"mappings", "schedules", "engagement"}:
        raise SourceError("Unsupported HubSpot source configuration.")
    mappings = config.get("mappings", [])
    if len(mappings) != 1 or mappings[0].get("id") != "contacts" or mappings[0].get("feed") != "people":
        raise SourceError("HubSpot requires one contacts mapping.")
    mapping = mappings[0]
    if set(mapping) - {"id", "feed", "properties", "columns", "schedule"}:
        raise SourceError("Unsupported HubSpot mapping field.")
    props = mapping.get("properties", DEFAULT_PROPERTIES)
    if (not isinstance(props, list) or not 1 <= len(props) <= 200
            or any(not isinstance(p, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,199}", p) for p in props)):
        raise SourceError("Select between 1 and 200 valid contact properties.")
    columns = mapping.get("columns", {})
    if (not isinstance(columns, dict) or len(columns) > 250
            or any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 256 for k, v in columns.items())):
        raise SourceError("Invalid HubSpot column mapping.")
    if type(config.get("engagement", False)) is not bool:
        raise SourceError("Engagement must be enabled or disabled.")
    if mapping.get("schedule"):
        validate_schedule(mapping["schedule"])


def validate_destination(config, credentials):
    from app.source_connectors import validate_schedule
    validate_token(credentials)
    if (set(config) - {"list_name", "list_id", "segment_id", "schedule"}
            or not isinstance(config.get("list_name"), str) or not config["list_name"].strip()
            or len(config["list_name"]) > 200 or type(config.get("segment_id")) is not int
            or config["segment_id"] < 1):
        raise SourceError("Choose a segment and a HubSpot static-list name.")
    if config.get("list_id") and not str(config["list_id"]).isdigit():
        raise SourceError("HubSpot list ID must be numeric.")
    if config.get("schedule"):
        validate_schedule(config["schedule"])


class HubSpot:
    def __init__(self, credentials, *, transport=None, sleep=time.sleep):
        validate_token(credentials)
        self.token = credentials["access_token"]
        self.sleep, self.next_request = sleep, 0.0
        self.client = httpx.Client(base_url="https://api.hubapi.com", timeout=45,
                                   follow_redirects=False, transport=transport,
                                   headers={"Authorization": f"Bearer {self.token}"})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def request(self, method, path, *, body=None, params=None, allow_missing=False):
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise SourceError("HubSpot requests must use the fixed API origin.")
        for attempt in range(7):
            self.sleep(max(0, self.next_request - time.monotonic()))
            try:
                response = self.client.request(method, path, json=body, params=params)
            except httpx.HTTPError:
                if attempt == 6:
                    raise SourceError("HubSpot network request failed; retry resumes saved checkpoints.") from None
                self.sleep(min(60, 2 ** attempt))
                continue
            self.next_request = time.monotonic() + 0.21  # Search API: at most five requests/second.
            headers = response.headers
            if headers.get("X-HubSpot-RateLimit-Daily-Remaining") == "0":
                raise SourceError("HubSpot daily API allowance is exhausted; resume after the quota resets.")
            if headers.get("X-HubSpot-RateLimit-Remaining") == "0":
                try:
                    self.next_request = time.monotonic() + float(headers.get("X-HubSpot-RateLimit-Interval-Milliseconds", "10000")) / 1000
                except ValueError:
                    self.next_request = time.monotonic() + 10
            if response.status_code == 404 and allow_missing:
                return None
            if response.status_code in {429, 500, 502, 503, 504}:
                retry = headers.get("Retry-After", "")
                try:
                    delay = float(retry)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError):
                        delay = min(60, 2 ** attempt)
                if delay > 120 or attempt == 6:
                    raise SourceError("HubSpot rate limit or temporary outage; retry resumes the current page.")
                self.sleep(max(1, delay))
                continue
            if response.status_code == 401:
                raise SourceError("HubSpot token is invalid or expired. Replace the saved private app token.")
            if response.status_code == 403:
                raise SourceError("HubSpot denied this feature. Test connection and grant its missing scopes.")
            if not 200 <= response.status_code < 300:
                raise SourceError(f"HubSpot rejected the request (HTTP {response.status_code}); check configuration.")
            if not response.content:
                return {}
            try:
                data = response.json()
            except ValueError:
                raise SourceError("HubSpot returned an invalid API response.") from None
            if isinstance(data, list) and method == "PUT" and "/memberships/" in path:
                return data
            if not isinstance(data, dict):
                raise SourceError("HubSpot returned an unexpected API response.")
            if data.get("status") in {"PENDING", "PROCESSING", "CANCELED", "CANCELLED"}:
                raise SourceError("HubSpot batch has not completed; no checkpoint was advanced.")
            if data.get("errors") or response.status_code == 207:
                raise SourceError("HubSpot batch had incomplete results; no checkpoint was advanced.")
            return data
        raise SourceError("HubSpot request could not complete.")

    def info(self):
        data = self.request("POST", "/oauth/v2/private-apps/get/access-token-info", body={"tokenKey": self.token})
        scopes = data.get("scopes", [])
        if not data.get("hubId") or not isinstance(scopes, list):
            raise SourceError("HubSpot did not return account and scope information.")
        return {"account": {"hub_id": data["hubId"], "app_id": data.get("appId")},
                "scopes": scopes, "missing_scopes": missing_scopes(scopes)}

    def contacts(self, since, until, after_id, properties):
        # Keyset ID paging keeps every request below CRM search's 10k paging
        # ceiling, including more than 10k contacts with the same timestamp.
        filters = [{"propertyName": "lastmodifieddate", "operator": "GTE", "value": str(since)},
                   {"propertyName": "lastmodifieddate", "operator": "LT", "value": str(until)},
                   {"propertyName": "hs_object_id", "operator": "GT", "value": str(after_id)}]
        page = self.request("POST", "/crm/v3/objects/contacts/search",
                            body={"filterGroups": [{"filters": filters}], "sorts": ["hs_object_id"],
                                  "properties": ["hs_object_id"], "limit": 100})
        ids = [str(item["id"]) for item in page.get("results", [])]
        if not ids:
            return []
        data = self.request("POST", "/crm/v3/objects/contacts/batch/read",
                            body={"properties": list(dict.fromkeys([*properties, "hs_object_id", "lastmodifieddate", "email", "hs_email_optout"])),
                                  "inputs": [{"id": identifier} for identifier in ids]})
        results = data.get("results", [])
        if {str(row["id"]) for row in results} != set(ids):
            raise SourceError("HubSpot contacts batch omitted records; retry the current page.")
        return sorted(results, key=lambda item: int(item["id"]))

    def preferences(self, emails, scopes):
        if "communication_preferences.statuses.batch.read" in scopes:
            data = self.request("POST", "/communication-preferences/v4/statuses/batch/read",
                                body={"inputs": emails}, params={"channel": "EMAIL"})
            result = {item["subscriberIdString"].lower(): item["statuses"]
                      for item in data.get("results", [])}
            if set(result) != {email.lower() for email in emails}:
                raise SourceError("HubSpot preference batch omitted emails; retry the current page.")
        else:
            result = {email.lower(): self.request("GET", f"/communication-preferences/v4/statuses/{quote(email, safe='')}",
                                                 params={"channel": "EMAIL"})["results"] for email in emails}
        for statuses in result.values():
            if not isinstance(statuses, list) or any(
                item.get("status") not in {"SUBSCRIBED", "UNSUBSCRIBED", "NOT_SPECIFIED"}
                or (item.get("status") != "NOT_SPECIFIED" and not item.get("timestamp")) for item in statuses
            ):
                raise SourceError("HubSpot returned incomplete subscription evidence.")
        return result
