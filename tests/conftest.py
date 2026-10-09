"""FakeER: an in-memory stand-in for erclient.ERClient covering the calls we make."""

import json
from dataclasses import dataclass

import pytest
from erclient.er_errors import ERClientNotFound


@dataclass
class FakeResponse:
    """What erclient._get(return_response=True) hands back: raw requests-shaped."""

    body: object
    status_code: int = 200
    content_type: str = "application/json"
    date: str = "Fri, 09 Oct 2026 09:00:00 GMT"
    text_override: str | None = None

    @property
    def text(self) -> str:
        if self.text_override is not None:
            return self.text_override
        return json.dumps({"data": self.body, "status": {"code": self.status_code}})

    @property
    def headers(self) -> dict:
        return {"Date": self.date, "Content-Type": self.content_type}


@pytest.fixture(autouse=True)
def _isolated_token_cache(tmp_path, monkeypatch):
    """Point the token cache at a per-test directory so tests never read or
    write the developer's real ~/.config/er-events (so the developer's default
    profile never leaks in), and drop any ER_* selection the developer's shell
    has exported, so tests see the same clean environment everywhere."""
    monkeypatch.setenv("ER_EVENTS_CONFIG_DIR", str(tmp_path / "er-events-config"))
    for var in ("ER_PROFILE", "ER_SERVER", "ER_USERNAME", "ER_PASSWORD", "ER_TOKEN"):
        monkeypatch.delenv(var, raising=False)


class FakeER:
    def __init__(self, categories=None, event_types=None, choices=None):
        self.categories = categories or []
        self.event_types = event_types or []
        # set by tests that need the v2 listing to differ from v1 (das serves
        # them separately); None means "same as event_types" for the many
        # authoring tests that don't care
        self.event_types_v2 = None
        self.choices = choices or {}  # field name -> list[dict]
        self.calls = []  # (method, ...) tuples, appended for every call
        self.auth = None  # token dict, set by token_store.apply_to_client
        self.auth_expires = None
        self.me = {"username": "chris", "id": "user-1"}
        # path (or absolute next-URL) -> the literal response body erclient._get would return
        self.responses: dict = {}
        # what GET /status says about the site clock (clock.fetch_clock reads it raw)
        self.status = {"server_timezone_name": "Africa/Nairobi", "server_timezone": "EAT"}
        self.date_header = "Fri, 09 Oct 2026 09:00:00 GMT"

    def auth_headers(self):
        self.calls.append(("auth_headers",))
        return {"Authorization": "Bearer fake"}

    def get_me(self):
        self.calls.append(("get_me",))
        return self.me

    # --- categories ---
    def get_event_categories(self, include_inactive=False):
        self.calls.append(("get_event_categories",))
        return self.categories

    def post_event_category(self, data):
        self.calls.append(("post_event_category", data))
        return data

    def patch_event_category(self, data):
        self.calls.append(("patch_event_category", data))
        return data

    # --- event types ---
    def get_event_types(self, include_inactive=False, include_schema=False, version="v1.0"):
        self.calls.append(("get_event_types", version))
        if version == "v2.0" and self.event_types_v2 is not None:
            return self.event_types_v2
        return self.event_types

    def post_event_type(self, event_type, version="v1.0"):
        self.calls.append(("post_event_type", event_type, version))
        return event_type

    def patch_event_type(self, event_type, version="v1.0"):
        self.calls.append(("patch_event_type", event_type, version))
        return event_type

    def _api_root(self, version="v1.0"):
        return f"https://fake.pamdas.org/api/{version}"

    # --- generic path methods (choices) ---
    def _get(self, path, base_url=None, params=None, max_retries=5, **kwargs):
        if kwargs.get("return_response"):
            # recorded under its own name so index-based `_get` assertions in
            # tests are unaffected by the clock lookup
            self.calls.append(("_get_response", path, params))
            body = (
                self.responses.get("status", self.status)
                if path == "status"
                else self.responses[path]
            )
            if callable(body):
                return body(path, params=params)
            if isinstance(body, FakeResponse):
                return body
            return FakeResponse(body, date=self.date_header)
        if path == "user/me":
            self.calls.append(("get_me", max_retries))
            return self.me
        if path in self.responses:
            self.calls.append(("_get", path, params, base_url, max_retries))
            return self.responses[path]
        if path.startswith("activity/eventtypes/"):
            # unseeded v2 lookups 404, as das does for anything that isn't a
            # v2 type; the CLI then falls back to the v1 schema endpoint
            self.calls.append(("_get", path, params, base_url, max_retries))
            raise ERClientNotFound()
        if path.startswith("activity/events/schema/eventtype/"):
            # unseeded v1 schema: the type exists but has no schema, so it
            # accepts any details — keeps posting tests that don't care about
            # validation simple
            self.calls.append(("_get", path, params, base_url, max_retries))
            return None
        self.calls.append(("_get", path, params))
        field = (params or {}).get("field")
        if field is None:
            results = [r for recs in self.choices.values() for r in recs]
        else:
            results = list(self.choices.get(field, []))
        return {"results": results, "next": None}

    def _post(self, path, payload, **kwargs):
        self.calls.append(("_post", path, payload))
        return payload

    def _patch(self, path, payload, **kwargs):
        self.calls.append(("_patch", path, payload))
        return payload

    # --- events ---
    def post_event(self, event):
        self.calls.append(("post_event", event))
        return event

    # helpers for assertions
    def writes(self):
        return [
            c
            for c in self.calls
            if c[0]
            in (
                "post_event_category",
                "patch_event_category",
                "post_event_type",
                "patch_event_type",
                "_post",
                "_patch",
                "post_event",
            )
        ]
