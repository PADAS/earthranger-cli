"""ERClient construction and choices-endpoint helpers.

erclient has no first-class choices methods; we use its generic path methods
(_get/_post/_patch), the same pattern er-smart-sync uses in production.
"""

from __future__ import annotations

from erclient.client import ERClient
from erclient.er_errors import ERClientException

DEFAULT_CLIENT_ID = "das_web_client"
CHOICES_PATH = "choices"
CHOICE_MODEL = "activity.event"


def normalize_server(server: str) -> str:
    """Accept a bare site name (`sandbox`), a hostname (`sandbox.pamdas.org`,
    `localhost:8000`), or a full URL, and return `scheme://host[:port]`.

    Only a single DNS label gets the `.pamdas.org` shorthand; anything with a
    dot or a port is already a host, so appending the suffix would mangle it
    (`sandbox.pamdas.org` -> `sandbox.pamdas.org.pamdas.org`).
    """
    server = server.strip().rstrip("/")
    if server.startswith(("http://", "https://")):
        return server
    if "." in server or ":" in server:
        return f"https://{server}"
    return f"https://{server}.pamdas.org"


def make_client(*, server: str, username: str, password: str) -> ERClient:
    return ERClient(
        service_root=normalize_server(server),
        username=username,
        password=password,
        client_id=DEFAULT_CLIENT_ID,
    )


def make_token_client(*, server: str) -> ERClient:
    """Client with no credentials; the caller restores a cached token onto it
    (token_store.apply_to_client). client_id is still needed for refreshes."""
    return ERClient(
        service_root=normalize_server(server),
        client_id=DEFAULT_CLIENT_ID,
    )


def get_choices(client, field_name: str) -> list[dict]:
    """All Choice records for (model=activity.event, field=field_name), inactive included.

    ER validates the field= filter against field values already in the DB and
    400s for a never-seen name; that means "no records for this field yet".
    """
    try:
        page = client._get(
            CHOICES_PATH,
            params={
                "model": CHOICE_MODEL,
                "field": field_name,
                "include_inactive": True,
                "page_size": 200,
            },
            max_retries=0,
        )
    except ERClientException as e:
        if "is not one of the available choices" in str(e):
            return []
        raise
    return _collect_pages(client, page)


def get_all_choices(client) -> list[dict]:
    """All Choice records on model=activity.event, every field, inactive included."""
    page = client._get(
        CHOICES_PATH,
        params={"model": CHOICE_MODEL, "include_inactive": True, "page_size": 200},
        max_retries=0,
    )
    return _collect_pages(client, page)


def _collect_pages(client, page) -> list[dict]:
    results: list[dict] = []
    while True:
        if isinstance(page, dict) and "results" in page:
            results.extend(page["results"])
            next_url = page.get("next")
            if not next_url:
                break
            page = client._get(next_url, max_retries=0)
        elif isinstance(page, list):
            results.extend(page)
            break
        else:
            break
    return results


def post_choice(client, payload: dict) -> dict:
    return client._post(CHOICES_PATH, payload=payload)


def patch_choice(client, choice_id: str, payload: dict) -> dict:
    return client._patch(f"{CHOICES_PATH}/{choice_id}", payload=payload)
