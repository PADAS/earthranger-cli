"""Read helpers: turn erclient `_get` responses into a flat record list plus meta.

erclient's `_get` already strips the ER `{"data": ..., "status": ...}` envelope
and returns `data`. What is left is one of:

- a DRF page: `{"count": N, "next": <url|None>, "previous": ..., "results": [...]}`
- a plain list (e.g. /subjectgroups)
- a single object (a subject, /status, a GeoJSON FeatureCollection, ...)

`normalize_page` flattens any of those to `(records, next_url, count)`;
`follow_pages` walks `next` links; `fetch` does the first request and returns
the `{records, meta}` pieces every read command emits.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from requests.exceptions import ChunkedEncodingError, ConnectionError
from urllib3.exceptions import MaxRetryError, ProtocolError, ReadTimeoutError

# Shared with the transport policy in client.py: attempts after the first.
READ_RETRIES = 3
_sleep = time.sleep  # patched in tests


def _is_body_read_failure(exc: BaseException) -> bool:
    """A GET whose headers arrived but whose body did not.

    requests consumes the body *after* the adapter's retry loop has returned
    (Session.send -> r.content), so a connection dropped mid-body surfaces as
    ChunkedEncodingError (ProtocolError) or ConnectionError(ReadTimeoutError)
    with no transport retry. A ConnectionError wrapping MaxRetryError is the
    opposite case — the transport already retried and gave up — and must not
    be retried again here.
    """
    if isinstance(exc, ChunkedEncodingError):
        return True
    if isinstance(exc, ConnectionError):
        cause = exc.args[0] if exc.args else None
        return isinstance(cause, (ProtocolError, ReadTimeoutError)) and not isinstance(
            cause, MaxRetryError
        )
    return False


def get_json(client, path: str, **kwargs: Any) -> Any:
    """`client._get(path, max_retries=0, **kwargs)` with a bounded retry for
    body-read failures, which the transport-level policy cannot see. Same
    schedule as that policy (0 s, 2 s, 4 s) and the same stderr note."""
    for attempt in range(READ_RETRIES + 1):
        try:
            return client._get(path, max_retries=0, **kwargs)
        except (ChunkedEncodingError, ConnectionError) as exc:
            if attempt == READ_RETRIES or not _is_body_read_failure(exc):
                raise
            wait = 0 if attempt == 0 else 2**attempt
            print(
                f"note: {type(exc).__name__} while reading GET {path}; "
                f"retrying ({attempt + 1} of {READ_RETRIES})",
                file=sys.stderr,
            )
            _sleep(wait)
    raise AssertionError("unreachable")


DEFAULT_PAGE_SIZE = 100


def normalize_page(data: Any) -> tuple[list, str | None, int | None]:
    """Return (records, next_url, count) for one already-unwrapped response."""
    if data is None:
        return [], None, 0
    if isinstance(data, dict) and "results" in data:
        return (
            list(data.get("results") or []),
            data.get("next") or None,
            data.get("count"),
        )
    if isinstance(data, list):
        return list(data), None, len(data)
    return [data], None, 1


MAX_PAGES = 200  # otus's ceiling; right for events/subjects, raised per row where not
DEFAULT_CAP = object()  # "use MAX_PAGES as it is at call time" (tests patch MAX_PAGES)


def _page_keys(records: list) -> tuple | None:
    keys = tuple(r.get("id") for r in records if isinstance(r, dict))
    return keys if keys and len(keys) == len(records) and all(keys) else None


def follow_pages(
    client, page: Any, *, limit: int | None = None, max_pages=DEFAULT_CAP
) -> tuple[list, int, int | None, bool]:
    """Collect `page` and every page reachable through its `next` link.

    Stops as soon as `limit` records are in hand (no further requests) and
    trims any overshoot from the last page. Absolute `next` links use the
    configured API origin, preserving the server-provided path and query.
    Returns (records, pages_fetched, count_reported, truncated); `truncated`
    is True when the walk stopped at `max_pages` (MAX_PAGES by default; None
    means no cap) or because a `next` link repeated or led back to the first
    page (a server bug that would otherwise loop forever), and the caller says
    so in meta.
    """
    cap = MAX_PAGES if max_pages is DEFAULT_CAP else max_pages
    records, next_url, count = normalize_page(page)
    first_keys = _page_keys(records)  # page 1's URL is unknown here; its records are not
    pages = 1
    seen: set[str] = set()
    truncated = False
    while next_url and (limit is None or len(records) < limit):
        if (cap is not None and pages >= cap) or next_url in seen:
            truncated = True
            break
        seen.add(next_url)
        link = urlsplit(next_url)
        if link.netloc:
            origin = urlsplit(client._api_root())
            next_url = urlunsplit((origin.scheme, origin.netloc, link.path, link.query, ""))
        more, next_url, _ = normalize_page(get_json(client, next_url))
        if first_keys is not None and _page_keys(more) == first_keys:
            truncated = True  # a `next` that led back to page 1: stop before duplicating it
            break
        records.extend(more)
        pages += 1
    if limit is not None:
        records = records[:limit]
    return records, pages, count, truncated


def fetch(
    client,
    path: str,
    params: dict | None = None,
    *,
    paginate: bool = False,
    limit: int | None = None,
    version: str | None = None,
    unwrap: Callable[[Any], Any] | None = None,
    max_pages=DEFAULT_CAP,
) -> tuple[list, dict]:
    """GET `path` (relative to the API root) and return (records, meta).

    `params` entries whose value is None are dropped. When `paginate` is
    true, `page_size` defaults to DEFAULT_PAGE_SIZE, capped at `limit` when
    one is given, and `next` links are followed (see follow_pages).
    `version` selects a non-default API root (e.g. "v2.0" for subject
    tracks) via erclient's `_api_root`.
    """
    params = {k: v for k, v in (params or {}).items() if v is not None}
    # an endpoint with its own envelope isn't DRF-paginated, so page_size would be noise
    if paginate and unwrap is None and "page_size" not in params:
        params["page_size"] = min(limit, DEFAULT_PAGE_SIZE) if limit else DEFAULT_PAGE_SIZE
    base_url = client._api_root(version) if version else None
    page = get_json(client, path, base_url=base_url, params=params)
    if unwrap is not None:
        # endpoint-specific envelope (e.g. {"features": [...]}) that normalize_page
        # would otherwise treat as a single record
        page = unwrap(page)
    truncated = False
    if paginate:
        records, pages, count, truncated = follow_pages(
            client, page, limit=limit, max_pages=max_pages
        )
    else:
        records, _, count = normalize_page(page)
        pages = 1
    meta: dict = {"total": len(records), "pages": pages}
    if count is not None:
        meta["count_reported"] = count
    if truncated:
        meta["truncated"] = True
        meta["note"] = (
            f"stopped after {pages} page(s); the result is a floor, not the total. "
            "Narrow the query (--since/--until, --limit) or raise --page-size."
        )
    return records, meta


def fetch_count(
    client, path: str, params: dict | None = None, *, version: str | None = None
) -> int | None:
    """The server's own total for a query in one request: DRF reports `count`
    beside the first page, so ask for one record and read the envelope. None
    when the endpoint is a bare list or a single object (no count to read);
    callers skip this for endpoints with their own envelope."""
    params = {k: v for k, v in (params or {}).items() if v is not None}
    params["page_size"] = 1
    base_url = client._api_root(version) if version else None
    page = get_json(client, path, base_url=base_url, params=params)
    if isinstance(page, dict) and "results" in page:
        return page.get("count")
    return None


def fetch_text(client, path: str, params: dict | None = None) -> tuple[str, str, bytes]:
    """GET a non-JSON body (the CSV exports). Returns (text, content_type, raw):
    the text for stdout and counting, the raw bytes for a file written as sent.
    erclient's `return_response=True` hands back the raw response on 2xx and
    still raises its typed errors on 401/403/404; get_json adds the same
    bounded body-read retry every JSON read gets."""
    params = {k: v for k, v in (params or {}).items() if v is not None}
    response = get_json(client, path, params=params, return_response=True)
    content_type = response.headers.get("Content-Type", "")
    # requests decodes text/* with no charset as ISO-8859-1; das's CSV is UTF-8
    match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    encoding = match.group(1) if match else "utf-8"
    try:
        body = response.content.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        body = response.content.decode("utf-8", errors="replace")
    return body, content_type, response.content
