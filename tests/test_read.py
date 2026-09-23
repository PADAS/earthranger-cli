from unittest.mock import Mock

from earthranger_cli.read import fetch, follow_pages, normalize_page


def test_normalize_drf_page():
    recs, nxt, cnt = normalize_page(
        {"count": 2, "next": "https://x/next", "previous": None, "results": [{"id": 1}, {"id": 2}]}
    )
    assert [r["id"] for r in recs] == [1, 2]
    assert nxt == "https://x/next"
    assert cnt == 2


def test_normalize_plain_list():
    recs, nxt, cnt = normalize_page([{"id": 1}, {"id": 2}])
    assert len(recs) == 2 and nxt is None and cnt == 2


def test_normalize_single_object_becomes_one_record():
    recs, nxt, cnt = normalize_page({"server_time": "2026-09-23T00:00:00Z"})
    assert recs == [{"server_time": "2026-09-23T00:00:00Z"}]
    assert nxt is None and cnt == 1


def test_normalize_none_is_empty():
    assert normalize_page(None) == ([], None, 0)


def test_follow_pages_walks_next_and_counts_pages():
    client = Mock()
    client._get.side_effect = [{"count": 3, "next": None, "results": [{"id": "c"}]}]
    first = {"count": 3, "next": "https://x/subjects/?page=2", "results": [{"id": "a"}, {"id": "b"}]}
    recs, pages, count = follow_pages(client, first)
    assert [r["id"] for r in recs] == ["a", "b", "c"]
    assert pages == 2 and count == 3
    client._get.assert_called_once_with("https://x/subjects/?page=2", max_retries=0)


def test_follow_pages_stops_at_limit_without_extra_requests():
    client = Mock()
    first = {"count": 5, "next": "https://x/?page=2", "results": [{"id": "a"}, {"id": "b"}]}
    recs, pages, _ = follow_pages(client, first, limit=2)
    assert [r["id"] for r in recs] == ["a", "b"]
    assert pages == 1
    client._get.assert_not_called()


def test_follow_pages_trims_overshoot_to_limit():
    client = Mock()
    client._get.side_effect = [{"count": 4, "next": None, "results": [{"id": "c"}, {"id": "d"}]}]
    first = {"count": 4, "next": "https://x/?page=2", "results": [{"id": "a"}, {"id": "b"}]}
    recs, pages, _ = follow_pages(client, first, limit=3)
    assert [r["id"] for r in recs] == ["a", "b", "c"]
    assert pages == 2


def test_fetch_list_adds_default_page_size_and_drops_none_params():
    client = Mock()
    client._get.return_value = {"count": 1, "next": None, "results": [{"id": "a"}]}
    records, meta = fetch(client, "subjects", {"name": "Najin", "bbox": None}, paginate=True)
    assert records == [{"id": "a"}]
    assert meta == {"total": 1, "pages": 1, "count_reported": 1}
    client._get.assert_called_once_with(
        "subjects", base_url=None, params={"name": "Najin", "page_size": 100}, max_retries=0
    )


def test_fetch_respects_explicit_page_size():
    client = Mock()
    client._get.return_value = {"count": 0, "next": None, "results": []}
    fetch(client, "subjects", {"page_size": 5}, paginate=True)
    assert client._get.call_args.kwargs["params"] == {"page_size": 5}


def test_fetch_get_does_not_paginate_or_add_page_size():
    client = Mock()
    client._get.return_value = {"id": "s1", "name": "Najin"}
    records, meta = fetch(client, "subject/s1", paginate=False)
    assert records == [{"id": "s1", "name": "Najin"}]
    assert meta == {"total": 1, "pages": 1, "count_reported": 1}
    client._get.assert_called_once_with("subject/s1", base_url=None, params={}, max_retries=0)


def test_fetch_version_uses_client_api_root():
    client = Mock()
    client._api_root.return_value = "https://x.pamdas.org/api/v2.0"
    client._get.return_value = {"type": "FeatureCollection", "features": []}
    fetch(client, "subject/s1/tracks", {"since": "2026-01-01"}, paginate=False, version="v2.0")
    client._api_root.assert_called_once_with("v2.0")
    assert client._get.call_args.kwargs["base_url"] == "https://x.pamdas.org/api/v2.0"


def test_fetch_meta_omits_count_reported_when_unknown():
    client = Mock()
    client._get.return_value = {"results": [{"id": "a"}], "next": None}  # no "count"
    _, meta = fetch(client, "choices", paginate=True)
    assert meta == {"total": 1, "pages": 1}


def test_fetch_caps_default_page_size_at_limit():
    client = Mock()
    client._get.return_value = {"count": 0, "next": None, "results": []}
    fetch(client, "subjects", paginate=True, limit=5)
    assert client._get.call_args.kwargs["params"] == {"page_size": 5}
    client._get.reset_mock()
    fetch(client, "subjects", {"page_size": 50}, paginate=True, limit=5)
    assert client._get.call_args.kwargs["params"] == {"page_size": 50}
