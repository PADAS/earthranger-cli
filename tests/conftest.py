"""FakeER: an in-memory stand-in for erclient.ERClient covering the calls we make."""


class FakeER:
    def __init__(self, categories=None, event_types=None, choices=None):
        self.categories = categories or []
        self.event_types = event_types or []
        self.choices = choices or {}  # field name -> list[dict]
        self.calls = []  # (method, ...) tuples, appended for every call

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
        return self.event_types

    def post_event_type(self, event_type, version="v1.0"):
        self.calls.append(("post_event_type", event_type, version))
        return event_type

    def patch_event_type(self, event_type, version="v1.0"):
        self.calls.append(("patch_event_type", event_type, version))
        return event_type

    # --- generic path methods (choices) ---
    def _get(self, path, params=None, max_retries=5, **kwargs):
        self.calls.append(("_get", path, params))
        field = (params or {}).get("field")
        return {"results": list(self.choices.get(field, [])), "next": None}

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
        return [c for c in self.calls if c[0] in (
            "post_event_category", "patch_event_category",
            "post_event_type", "patch_event_type", "_post", "_patch", "post_event",
        )]
