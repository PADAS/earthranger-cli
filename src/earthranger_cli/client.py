"""ERClient construction and choices-endpoint helpers.

erclient has no first-class choices methods; we use its generic path methods
(_get/_post/_patch), the same pattern er-smart-sync uses in production.
"""

from __future__ import annotations

import sys
from urllib.parse import urlsplit, urlunsplit

from erclient.client import ERClient
from erclient.er_errors import ERClientException
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .read import READ_RETRIES, follow_pages, get_json

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


# Transient failures a read may simply try again: rate limiting and the 5xx
# family, plus connection/read errors. Three retries with exponential backoff
# (0 s, 2 s, 4 s); a Retry-After header is honoured up to a short cap.
READ_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# urllib3 would otherwise sleep for up to 6 h on a server-supplied Retry-After
READ_RETRY_AFTER_MAX = 30


class _NotingRetry(Retry):
    """urllib3 Retry that says so on stderr each time it retries, so a command
    that is waiting out a 503 doesn't look hung, and the eventual error
    (which erclient words as 'after 1 tries') isn't the whole story."""

    def get_retry_after(self, response):
        # capped here rather than via the `retry_after_max` constructor argument,
        # which only exists from urllib3 2.6.3
        wait = super().get_retry_after(response)
        return None if wait is None else min(wait, READ_RETRY_AFTER_MAX)

    def increment(self, method=None, url=None, response=None, error=None, *args, **kwargs):
        new = super().increment(method, url, response, error, *args, **kwargs)
        reason = f"HTTP {response.status}" if response is not None else type(error).__name__
        print(
            f"note: {reason} on {method} {url}; retrying ({len(new.history)} of {READ_RETRIES})",
            file=sys.stderr,
        )
        return new


def _read_retry_policy() -> Retry:
    return _NotingRetry(
        total=READ_RETRIES,
        # urllib3 counts DNS failures and connection-refused as connect
        # timeouts: one connect retry (no backoff before the first) keeps a
        # real blip covered without making a mistyped --server wait 6 s
        connect=1,
        read=READ_RETRIES,
        status=READ_RETRIES,
        # urllib3 does NOT consult allowed_methods for its "other" error
        # category (e.g. an SSL error while receiving headers after the
        # request body went out), so a POST could be replayed through it.
        # Disable that category entirely — those errors are rarely transient.
        other=0,
        backoff_factor=1,
        status_forcelist=READ_RETRY_STATUSES,
        # GET only: a timed-out POST/PATCH may have landed, so writes are never
        # replayed. (erclient's own default adapter retried 502s for writes
        # too; this replaces it.)
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        # hand the final response back to erclient so its own status mapping
        # (401 -> ERClientBadCredentials, 404 -> ERClientNotFound, ...) applies
        raise_on_status=False,
    )


def with_read_retries(client: ERClient) -> ERClient:
    """Mount a GET-only retry policy on the client's HTTP session (in place).

    Lives at the transport layer so every read the CLI makes — resource
    commands, pagination, schema fetches, choices — gets it with no per-call
    code, and erclient's own retry loop stays disabled (`max_retries=0`) so
    nothing retries twice.
    """
    adapter = HTTPAdapter(max_retries=_read_retry_policy())
    # requests resolves adapters by longest matching prefix, and a Session is
    # born with stock (no-retry) adapters on "http://" / "https://" while
    # erclient adds its own on "http" / "https". Clear all of them and mount
    # exactly one per scheme, so nothing can shadow the policy.
    client._http_session.adapters.clear()
    client._http_session.mount("http://", adapter)
    client._http_session.mount("https://", adapter)
    return client


def make_client(*, server: str, username: str, password: str) -> ERClient:
    return with_read_retries(
        ERClient(
            service_root=normalize_server(server),
            username=username,
            password=password,
            client_id=DEFAULT_CLIENT_ID,
        )
    )


def make_token_client(*, server: str) -> ERClient:
    """Client with no credentials; the caller restores a cached token onto it
    (token_store.apply_to_client). client_id is still needed for refreshes."""
    return with_read_retries(
        ERClient(
            service_root=normalize_server(server),
            client_id=DEFAULT_CLIENT_ID,
        )
    )


def make_static_token_client(*, server: str, token: str) -> ERClient:
    """Client authenticated with a pre-issued bearer token (--token / ER_TOKEN).

    erclient's `token=` kwarg preloads `auth` and sets `auth_expires` to 2099,
    so auth_headers() never attempts a refresh or password login; an invalid
    token surfaces as the API's 401 on the first request.
    """
    return with_read_retries(
        ERClient(
            service_root=normalize_server(server),
            token=token,
            client_id=DEFAULT_CLIENT_ID,
        )
    )


def describe_error(e: ERClientException) -> str:
    """str(e) for erclient errors, which is literally 'None' for the ones erclient
    raises without a message (ERClientNotFound)."""
    msg = str(e)
    if msg in ("", "None"):
        return f"{type(e).__name__.removeprefix('ERClient')} (no details from the server)"
    return msg


def get_me(client) -> dict:
    """The authenticated user (/user/me/). erclient's own retry loop (five
    attempts, fixed 5 s sleeps) is disabled; the client's transport-level
    read policy (see with_read_retries: 0/2/4 s, Retry-After capped) still
    applies, so a credential check against a struggling site fails within
    seconds rather than ~25 s."""
    return get_json(client, "user/me")


def get_choices(client, field_name: str) -> list[dict]:
    """All Choice records for (model=activity.event, field=field_name), inactive included.

    ER validates the field= filter against field values already in the DB and
    400s for a never-seen name; that means "no records for this field yet".
    """
    try:
        page = get_json(
            client,
            CHOICES_PATH,
            params={
                "model": CHOICE_MODEL,
                "field": field_name,
                "include_inactive": True,
                "page_size": 200,
            },
        )
    except ERClientException as e:
        if "is not one of the available choices" in str(e):
            return []
        raise
    return _collect_pages(client, page)


def get_all_choices(client) -> list[dict]:
    """All Choice records on model=activity.event, every field, inactive included."""
    page = get_json(
        client,
        CHOICES_PATH,
        params={"model": CHOICE_MODEL, "include_inactive": True, "page_size": 200},
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
