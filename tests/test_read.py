from unittest.mock import Mock

import pytest

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
    client._api_root.return_value = "https://x/api/v1.0"
    client._get.side_effect = [{"count": 3, "next": None, "results": [{"id": "c"}]}]
    first = {
        "count": 3,
        "next": "https://x/subjects/?page=2",
        "results": [{"id": "a"}, {"id": "b"}],
    }
    recs, pages, count, _ = follow_pages(client, first)
    assert [r["id"] for r in recs] == ["a", "b", "c"]
    assert pages == 2 and count == 3
    client._get.assert_called_once_with("https://x/subjects/?page=2", max_retries=0)


def test_follow_pages_stops_at_limit_without_extra_requests():
    client = Mock()
    first = {"count": 5, "next": "https://x/?page=2", "results": [{"id": "a"}, {"id": "b"}]}
    recs, pages, _, _ = follow_pages(client, first, limit=2)
    assert [r["id"] for r in recs] == ["a", "b"]
    assert pages == 1
    client._get.assert_not_called()


def test_follow_pages_trims_overshoot_to_limit():
    client = Mock()
    client._api_root.return_value = "https://x/api/v1.0"
    client._get.side_effect = [{"count": 4, "next": None, "results": [{"id": "c"}, {"id": "d"}]}]
    first = {"count": 4, "next": "https://x/?page=2", "results": [{"id": "a"}, {"id": "b"}]}
    recs, pages, _, _ = follow_pages(client, first, limit=3)
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


@pytest.mark.parametrize(
    "next_url",
    [
        "http://internal:8000/api/v2.0/subjects/?page=2&state=a&state=b",
        "//internal:8000/api/v2.0/subjects/?page=2&state=a&state=b",
        "https://public.example:8443/api/v2.0/subjects/?page=2&state=a&state=b",
    ],
)
def test_pagination_uses_configured_origin_and_preserves_path_query(next_url):
    client = Mock()
    client._api_root.return_value = "https://public.example:8443/api/v1.0"
    client._get.return_value = {"results": [{"id": "b"}], "next": None}
    first = {"count": 2, "results": [{"id": "a"}], "next": next_url}
    records, pages, count, _ = follow_pages(client, first)
    assert records == [{"id": "a"}, {"id": "b"}]
    assert (pages, count) == (2, 2)
    client._get.assert_called_once_with(
        "https://public.example:8443/api/v2.0/subjects/?page=2&state=a&state=b",
        max_retries=0,
    )


def _body_failure_client(failures, page):
    """_get raises each exception in `failures` in turn, then returns `page`."""
    client = Mock()
    client._get.side_effect = list(failures) + [page]
    return client


def test_get_json_retries_a_body_read_failure_with_backoff(monkeypatch, capsys):
    from requests.exceptions import ChunkedEncodingError, ConnectionError
    from urllib3.exceptions import ProtocolError, ReadTimeoutError

    from earthranger_cli import read

    slept = []
    monkeypatch.setattr(read, "_sleep", slept.append)
    client = _body_failure_client(
        [
            ChunkedEncodingError(ProtocolError("Connection broken: IncompleteRead")),
            ConnectionError(ReadTimeoutError(None, "/x", "Read timed out")),
        ],
        {"count": 1, "next": None, "results": [{"id": "a"}]},
    )
    page = read.get_json(client, "subjects", params={"page_size": 100})
    assert page["results"] == [{"id": "a"}]
    assert client._get.call_count == 3
    assert slept == [0, 2]  # same schedule as the transport policy
    err = capsys.readouterr().err.splitlines()
    assert err == [
        "note: ChunkedEncodingError while reading GET subjects; retrying (1 of 3)",
        "note: ConnectionError while reading GET subjects; retrying (2 of 3)",
    ]


def test_get_json_does_not_retry_an_exhausted_transport(monkeypatch):
    from requests.exceptions import ConnectionError
    from urllib3.exceptions import MaxRetryError

    from earthranger_cli import read

    monkeypatch.setattr(read, "_sleep", lambda s: pytest.fail("must not sleep"))
    # the adapter already retried 0/2/4 s and gave up; retrying here would double it
    client = _body_failure_client([ConnectionError(MaxRetryError(None, "/x"))], {})
    with pytest.raises(ConnectionError):
        read.get_json(client, "subjects")
    assert client._get.call_count == 1


def test_get_json_gives_up_after_read_retries(monkeypatch):
    from requests.exceptions import ChunkedEncodingError
    from urllib3.exceptions import ProtocolError

    from earthranger_cli import read

    monkeypatch.setattr(read, "_sleep", lambda s: None)
    client = _body_failure_client([ChunkedEncodingError(ProtocolError("x"))] * 4, {})
    with pytest.raises(ChunkedEncodingError):
        read.get_json(client, "subjects")
    assert client._get.call_count == 4  # first try + READ_RETRIES


def test_fetch_and_pagination_go_through_get_json(monkeypatch):
    from earthranger_cli import read

    seen = []

    def fake_get_json(client, path, **kw):
        seen.append(path)
        if path == "subjects":
            return {
                "count": 2,
                "next": "https://x/api/v1.0/subjects/?page=2",
                "results": [{"id": "a"}],
            }
        return {"count": 2, "next": None, "results": [{"id": "b"}]}

    monkeypatch.setattr(read, "get_json", fake_get_json)
    client = Mock()
    client._api_root.return_value = "https://x/api/v1.0"
    records, _meta = read.fetch(client, "subjects", {}, paginate=True)
    assert [r["id"] for r in records] == ["a", "b"]
    assert seen == ["subjects", "https://x/api/v1.0/subjects/?page=2"]


from earthranger_cli import read


class _Client:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def _api_root(self, version="v1.0"):
        return f"https://fake.pamdas.org/api/{version}"

    def _get(self, path, base_url=None, params=None, max_retries=0, **kwargs):
        self.calls.append((path, params or {}))
        key = path.rsplit("/", 1)[-1] if path.startswith("http") else path
        return self.responses[key]


def test_follow_pages_stops_at_max_pages_and_marks_truncated(monkeypatch):
    monkeypatch.setattr(read, "MAX_PAGES", 3)
    client = _Client({f"p{i}": {"results": [{"i": i}], "next": f"p{i + 1}"} for i in range(1, 10)})
    records, pages, _, truncated = read.follow_pages(client, {"results": [{"i": 0}], "next": "p1"})
    assert [r["i"] for r in records] == [0, 1, 2]
    assert pages == 3
    assert truncated is True


def test_follow_pages_stops_when_next_repeats():
    client = _Client({"loop": {"results": [{"i": 1}], "next": "loop"}})
    records, pages, _, truncated = read.follow_pages(
        client, {"results": [{"i": 0}], "next": "loop"}
    )
    assert [r["i"] for r in records] == [0, 1]
    assert pages == 2
    assert truncated is True


def test_fetch_reports_truncation_in_meta(monkeypatch):
    monkeypatch.setattr(read, "MAX_PAGES", 1)
    client = _Client(
        {"things": {"count": 50, "results": [{"i": 0}], "next": "p1"}, "p1": {"results": []}}
    )
    _records, meta = read.fetch(client, "things", paginate=True)
    assert meta["truncated"] is True
    assert "200" not in meta["note"] and "page" in meta["note"]
    assert meta["count_reported"] == 50


def test_fetch_count_asks_for_one_record():
    client = _Client({"things": {"count": 1234, "results": [{"i": 0}], "next": "p1"}})
    assert read.fetch_count(client, "things", {"state": "active"}) == 1234
    assert client.calls[-1][1]["page_size"] == 1
    assert client.calls[-1][1]["state"] == "active"


def test_fetch_count_is_none_for_unpaginated_endpoints():
    client = _Client({"things": [{"i": 0}, {"i": 1}]})
    assert read.fetch_count(client, "things", {}) is None


def test_follow_pages_stops_when_a_page_repeats_the_first_page():
    # a next link pointing back at page 1 must not append page 1's records twice
    client = _Client({"p2": {"results": [{"id": "a"}, {"id": "b"}], "next": "p1"}})
    first = {"results": [{"id": "a"}, {"id": "b"}], "next": "p2"}
    records, _pages, _, truncated = read.follow_pages(client, first)
    assert [r["id"] for r in records] == ["a", "b"]
    assert truncated is True


def test_fetch_text_decodes_utf8_when_the_server_names_no_charset():
    # requests decodes text/* without a charset as ISO-8859-1; the export is UTF-8
    class _Resp:
        content = "id,who\ne1,José – Nyumbu\n".encode()
        text = content.decode("iso-8859-1")  # what requests would hand back
        headers = {"Content-Type": "text/csv"}  # noqa: RUF012

    class _C:
        def _get(self, path, **kwargs):
            return _Resp()

    body, kind, _raw = read.fetch_text(_C(), "x")
    assert body == "id,who\ne1,José – Nyumbu\n" and kind == "text/csv"


def test_fetch_text_honours_a_declared_charset():
    class _Resp:
        content = "id\nJosé\n".encode("latin-1")
        headers = {"Content-Type": "text/csv; charset=iso-8859-1"}  # noqa: RUF012

    class _C:
        def _get(self, path, **kwargs):
            return _Resp()

    assert read.fetch_text(_C(), "x")[0] == "id\nJosé\n"


def test_fetch_text_retries_a_dropped_body(monkeypatch):
    from requests.exceptions import ChunkedEncodingError

    monkeypatch.setattr(read, "_sleep", lambda s: None)

    class _Resp:
        content = b"id\n"
        headers = {"Content-Type": "text/csv"}  # noqa: RUF012

    class _C:
        calls = 0

        def _get(self, path, **kwargs):
            _C.calls += 1
            if _C.calls == 1:
                raise ChunkedEncodingError("dropped")
            return _Resp()

    assert read.fetch_text(_C(), "x")[0] == "id\n"
    assert _C.calls == 2


def test_fetch_text_also_returns_the_bytes_as_sent():
    class _Resp:
        content = "id\nJosé\n".encode("latin-1")
        headers = {"Content-Type": "text/csv; charset=iso-8859-1"}  # noqa: RUF012

    class _C:
        def _get(self, path, **kwargs):
            return _Resp()

    body, _kind, raw = read.fetch_text(_C(), "x")
    assert body == "id\nJosé\n" and raw == _Resp.content
