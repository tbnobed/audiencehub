"""Fixed-host Graph API adapter. Never log remote errors, tokens or lead data."""
import hashlib
import hmac
import json
import logging
import re
import time
import httpx
from app.meta_privacy import safe_capi


class _NoMetaTransportLogs(logging.Filter):
    def filter(self, record):
        # HTTPX logs complete query URLs (debug_token includes input_token).
        # Graph errors are surfaced through our sanitized adapter, never raw
        # provider transport logs. HTTPcore DEBUG can include auth headers.
        return "graph.facebook.com" not in record.getMessage()


logging.getLogger("httpx").addFilter(_NoMetaTransportLogs())
logging.getLogger("httpcore").setLevel(logging.WARNING)


class MetaError(ValueError):
    pass


class Meta:
    def __init__(self, config, credentials, ads=False):
        self.config, self.credentials = config, credentials
        self.token = credentials.get("ads_access_token") if ads else credentials.get("page_access_token")
        if not self.token:
            raise MetaError("Save a Meta advertising token." if ads else "Save a Page access token.")
        version = config.get("graph_version", "v25.0")
        if not re.fullmatch(r"v[0-9]{2}\.0", version):
            raise MetaError("Invalid pinned Meta Graph API version.")
        self.base = f"https://graph.facebook.com/{version}"
        self.client = httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False, trust_env=False)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.client.close()

    def request(self, method, path, params=None, body=None, token=None):
        method, path = method.upper(), path.rstrip("/")
        if not re.fullmatch(r"/[A-Za-z0-9_/-]+", path):
            raise MetaError("Invalid Graph resource path.")
        if path.endswith("/events"):
            safe_capi(body or params or {})
        selected = token or self.token
        params = dict(params or {})
        if self.credentials.get("app_secret"):
            params["appsecret_proof"] = hmac.new(self.credentials["app_secret"].encode(), selected.encode(),
                                               hashlib.sha256).hexdigest()
        attempts = 1 if method == "POST" and path.endswith("/customaudiences") else 6
        for attempt in range(attempts):
            try:
                response = self.client.request(method, self.base + path, params=params, json=body,
                                               headers={"Authorization": f"Bearer {selected}"})
                data = response.json()
            except (httpx.HTTPError, ValueError):
                if attempt == attempts - 1:
                    if attempts == 1:
                        raise MetaError("Meta audience creation outcome is unknown; check Ads Manager for an empty audience before retrying.") from None
                    raise MetaError("Meta request failed; retry resumes committed leads.") from None
                time.sleep(min(2 ** attempt, 30))
                continue
            error = data.get("error", {}) if isinstance(data, dict) else {}
            throttled = response.status_code == 429 or error.get("code") in {1, 2, 4, 17, 32, 613}
            if throttled or response.status_code >= 500 or error.get("is_transient") is True:
                if attempt == attempts - 1:
                    raise MetaError("Meta rate limit or service error; retry the run.")
                delay = response.headers.get("Retry-After", "")
                time.sleep(min(float(delay) if re.fullmatch(r"\d+(\.\d+)?", delay) else 2 ** attempt, 120))
                continue
            if response.status_code >= 300 or error or not isinstance(data, dict):
                raise MetaError(f"Meta rejected the request (HTTP {response.status_code}); check token, Page access and app permissions.")
            for header in ("x-app-usage", "x-page-usage", "x-ad-account-usage"):
                try:
                    usage = json.loads(response.headers.get(header, "{}"))
                    if max((float(v) for v in usage.values() if type(v) in (int, float)), default=0) >= 90:
                        time.sleep(5)
                except (ValueError, TypeError):
                    pass
            try:
                def high_usage(item):
                    if isinstance(item, list):
                        return any(high_usage(v) for v in item)
                    if isinstance(item, dict):
                        return any((k in {"call_count", "total_time", "total_cputime", "acc_id_util_pct"}
                                    and type(v) in (int, float) and v >= 90)
                                   or high_usage(v) for k, v in item.items())
                    return False
                if high_usage(json.loads(response.headers.get("x-business-use-case-usage", "{}"))):
                    time.sleep(5)
            except (ValueError, TypeError):
                pass
            return data
        raise MetaError("Meta request exhausted retry budget.")

    def pages(self, path, params=None):
        params = dict(params or {})
        for _ in range(100000):
            data = self.request("GET", path, params)
            yield from data.get("data", [])
            # Never follow Graph's arbitrary paging.next URL (often contains a token).
            paging = data.get("paging", {})
            cursor = paging.get("cursors", {}).get("after")
            if not paging.get("next") or not cursor:
                return
            if params.get("after") == cursor:
                raise MetaError("Meta returned a repeated pagination cursor.")
            params["after"] = cursor
        raise MetaError("Meta pagination limit reached.")
