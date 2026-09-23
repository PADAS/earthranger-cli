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

from typing import Any

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


def follow_pages(
    client, page: Any, *, limit: int | None = None
) -> tuple[list, int, int | None]:
    """Collect `page` and every page reachable through its `next` link.

    Stops as soon as `limit` records are in hand (no further requests) and
    trims any overshoot from the last page. `next` is the absolute URL ER
    returns; erclient's `_get` passes absolute URLs through untouched.
    Returns (records, pages_fetched, count_reported).
    """
    records, next_url, count = normalize_page(page)
    pages = 1
    while next_url and (limit is None or len(records) < limit):
        more, next_url, _ = normalize_page(client._get(next_url, max_retries=0))
        records.extend(more)
        pages += 1
    if limit is not None:
        records = records[:limit]
    return records, pages, count


def fetch(
    client,
    path: str,
    params: dict | None = None,
    *,
    paginate: bool = False,
    limit: int | None = None,
    version: str | None = None,
) -> tuple[list, dict]:
    """GET `path` (relative to the API root) and return (records, meta).

    `params` entries whose value is None are dropped. When `paginate` is
    true, `page_size` defaults to DEFAULT_PAGE_SIZE and `next` links are
    followed (see follow_pages). `version` selects a non-default API root
    (e.g. "v2.0" for subject tracks) via erclient's `_api_root`.
    """
    params = {k: v for k, v in (params or {}).items() if v is not None}
    if paginate and "page_size" not in params:
        params["page_size"] = DEFAULT_PAGE_SIZE
    base_url = client._api_root(version) if version else None
    page = client._get(path, base_url=base_url, params=params, max_retries=0)
    if paginate:
        records, pages, count = follow_pages(client, page, limit=limit)
    else:
        records, _, count = normalize_page(page)
        pages = 1
    meta: dict = {"total": len(records), "pages": pages}
    if count is not None:
        meta["count_reported"] = count
    return records, meta
