"""ERClient construction and choices-endpoint helpers.

erclient has no first-class choices methods; we use its generic path methods
(_get/_post/_patch), the same pattern er-smart-sync uses in production.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from erclient.client import ERClient
from erclient.er_errors import ERClientException

from .read import follow_pages

DEFAULT_CLIENT_ID = "das_web_client"
CHOICES_PATH = "choices"
CHOICE_MODEL = "activity.event"


class ServerError(ValueError):
    """A --server value that can't be turned into an http(s) URL."""


def normalize_server(server: str) -> str:
    """Accept a bare site name (`sandbox`), a hostname (`sandbox.pamdas.org`,
    `localhost:8000`), or an http(s) URL, and return an http(s) URL.

    Only a single DNS label gets the `.pamdas.org` shorthand; anything with a
    dot or a port is already a host, so appending the suffix would mangle it
    (`sandbox.pamdas.org` -> `sandbox.pamdas.org.pamdas.org`). A URL's path is
    kept (ERClient strips `/api...` itself), as are any query and fragment; a
    trailing slash is removed.

    The result is canonical enough to compare for identity (profile <-> flag,
    cached session <-> server): scheme and host[:port] are lower-cased, the
    path is left alone.
    """
    if not isinstance(server, str):
        # hand-edited config.json can hold anything; keep it on the ServerError
        # path that _same_server / _api_errors already tolerate
        raise ServerError(
            f"invalid server {server!r}: use a site name, hostname, or http(s):// URL"
        )
    server = server.strip()
    if not server:
        raise ServerError("server is required: a site name, hostname, or http(s):// URL")
    invalid = ServerError(
        f"invalid server {server!r}: use a site name, hostname, or http(s):// URL"
    )
    if any(c.isspace() for c in server):
        # checked on the raw input: urlsplit silently drops embedded
        # tab/CR/LF (bpo-43882), which would turn 'sandbox\ttab' into a
        # different host instead of an error
        raise invalid
    had_scheme = "://" in server
    parts = urlsplit(server if had_scheme else f"https://{server}")
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if scheme not in ("http", "https") or not netloc:
        raise invalid
    try:
        parts.port  # noqa: B018 — raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        raise invalid from None
    if not had_scheme and "." not in netloc and ":" not in netloc:
        netloc = f"{netloc}.pamdas.org"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), parts.query, parts.fragment))


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


def make_static_token_client(*, server: str, token: str) -> ERClient:
    """Client authenticated with a pre-issued bearer token (--token / ER_TOKEN).

    erclient's `token=` kwarg preloads `auth` and sets `auth_expires` to 2099,
    so auth_headers() never attempts a refresh or password login; an invalid
    token surfaces as the API's 401 on the first request.
    """
    return ERClient(
        service_root=normalize_server(server),
        token=token,
        client_id=DEFAULT_CLIENT_ID,
    )


def describe_error(e: ERClientException) -> str:
    """str(e) for erclient errors, which is literally 'None' for the ones erclient
    raises without a message (ERClientNotFound)."""
    msg = str(e)
    if msg in ("", "None"):
        return f"{type(e).__name__.removeprefix('ERClient')} (no details from the server)"
    return msg


def get_me(client) -> dict:
    """The authenticated user (/user/me/), as a one-shot probe: no retries, so a
    credential check against a struggling site fails in one round-trip rather
    than erclient's default five attempts with 5 s sleeps."""
    return client._get("user/me", max_retries=0)


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
    """All records reachable from `page` (the first response) by following `next`."""
    records, _pages, _count = follow_pages(client, page)
    return records


def post_choice(client, payload: dict) -> dict:
    return client._post(CHOICES_PATH, payload=payload)


def patch_choice(client, choice_id: str, payload: dict) -> dict:
    return client._patch(f"{CHOICES_PATH}/{choice_id}", payload=payload)
