from unittest.mock import Mock

import pytest
from erclient.er_errors import ERClientException

from earthranger_cli.client import (
    ServerError,
    get_choices,
    get_me,
    make_client,
    make_static_token_client,
    make_token_client,
    normalize_server,
    patch_choice,
    post_choice,
)


def test_normalize_server_bare_name():
    assert normalize_server("myreserve") == "https://myreserve.pamdas.org"


def test_normalize_server_full_url_and_trailing_slash():
    assert normalize_server("https://x.example.org/") == "https://x.example.org"


def test_make_client_wires_credentials():
    client = make_client(server="myreserve", username="u", password="p")
    assert client.service_root == "https://myreserve.pamdas.org"
    assert client.username == "u"
    assert client.password == "p"
    assert client.client_id == "das_web_client"
    assert client.token_url == "https://myreserve.pamdas.org/oauth2/token"


def test_get_choices_pages_through_results():
    client = Mock()
    client._api_root.return_value = "https://x/api/v1.0"
    client._get.side_effect = [
        {"results": [{"value": "a"}], "next": "https://x/choices?page=2"},
        {"results": [{"value": "b"}], "next": None},
    ]
    result = get_choices(client, "t1_species")
    assert [c["value"] for c in result] == ["a", "b"]
    first_call = client._get.call_args_list[0]
    assert first_call.args[0] == "choices"
    assert first_call.kwargs["params"] == {
        "model": "activity.event",
        "field": "t1_species",
        "include_inactive": True,
        "page_size": 200,
    }
    assert client._get.call_args_list[1].args[0] == "https://x/choices?page=2"


def test_get_choices_unknown_field_400_means_empty():
    client = Mock()
    client._get.side_effect = ERClientException(
        "Failed to call ER web service. 'field': 't1_species' is not one of the available choices."
    )
    assert get_choices(client, "t1_species") == []


def test_get_choices_other_errors_propagate():
    client = Mock()
    client._get.side_effect = ERClientException("500 server error")
    with pytest.raises(ERClientException):
        get_choices(client, "t1_species")


def test_post_and_patch_choice_paths():
    client = Mock()
    post_choice(client, {"value": "a"})
    client._post.assert_called_once_with("choices", payload={"value": "a"})
    patch_choice(client, "abc123", {"is_active": False})
    client._patch.assert_called_once_with("choices/abc123", payload={"is_active": False})


def test_get_all_choices_pages_without_field_filter():
    client = Mock()
    client._api_root.return_value = "https://x/api/v1.0"
    client._get.side_effect = [
        {"results": [{"value": "a", "field": "f1"}], "next": "https://x/choices?page=2"},
        {"results": [{"value": "b", "field": "f2"}], "next": None},
    ]
    from earthranger_cli.client import get_all_choices

    result = get_all_choices(client)
    assert [c["value"] for c in result] == ["a", "b"]
    first = client._get.call_args_list[0]
    assert first.kwargs["params"] == {
        "model": "activity.event",
        "include_inactive": True,
        "page_size": 200,
    }


def test_make_static_token_client_preloads_bearer_and_never_refreshes():
    client = make_static_token_client(server="myreserve", token="tok-1")
    assert client.service_root == "https://myreserve.pamdas.org"
    assert client.auth == {"token_type": "Bearer", "access_token": "tok-1"}
    assert client.auth_expires.year == 2099
    assert client.username is None and client.password is None
    # no network: auth_headers must not try to log in
    assert client.auth_headers()["Authorization"] == "Bearer tok-1"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("sandbox", "https://sandbox.pamdas.org"),  # bare site name: shorthand applies
        ("sandbox.pamdas.org", "https://sandbox.pamdas.org"),  # hostname: no suffix
        ("SANDBOX.pamdas.org/", "https://sandbox.pamdas.org"),  # host lower-cased: it's an identity
        ("er.example.org", "https://er.example.org"),  # non-pamdas host
        ("localhost:8000", "https://localhost:8000"),  # host:port
        ("http://localhost:8000", "http://localhost:8000"),  # explicit scheme kept
        ("https://sandbox.pamdas.org/", "https://sandbox.pamdas.org"),
        ("HTTPS://sandbox.pamdas.org", "https://sandbox.pamdas.org"),  # scheme case-insensitive
        ("Http://localhost:8000/", "http://localhost:8000"),
        ("https://er.example.org/api/v1.0/", "https://er.example.org/api/v1.0"),  # path kept
        ("https://ER.Example.org/Api/V1.0", "https://er.example.org/Api/V1.0"),  # path case kept
        ("Sandbox", "https://sandbox.pamdas.org"),
        ("sandbox/api", "https://sandbox.pamdas.org/api"),  # suffix goes on the host, not the path
        ("https://host?Token=ABC", "https://host?Token=ABC"),  # query kept, not lower-cased
        ("https://Host/p?Q=1#F", "https://host/p?Q=1#F"),
        ("https://host", "https://host"),  # explicit scheme: no shorthand
    ],
)
def test_normalize_server_accepts_site_name_hostname_or_url(given, expected):
    assert normalize_server(given) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "/",
        "ftp://host",
        "HTTPS://",
        "https:// ",
        "sandbox:abc",  # non-numeric port
        "https://host:99999",  # port out of range
        "sand box.pamdas.org",  # whitespace in the authority
        "sandbox\ttab",  # urlsplit would silently drop the tab (bpo-43882)
        "sand\nbox.pamdas.org",
        "https://host\r.pamdas.org",
    ],
)
def test_normalize_server_rejects_blank_and_non_http_schemes(bad):
    with pytest.raises(ServerError, match="site name, hostname, or http\\(s\\):// URL"):
        normalize_server(bad)


def test_get_me_disables_erclients_own_retry_loop():
    # transport-level read retries (0/2/4 s) still apply; erclient's five
    # fixed 5 s sleeps must not stack on top of them
    client = Mock()
    client._get.return_value = {"username": "chris"}
    assert get_me(client) == {"username": "chris"}
    client._get.assert_called_once_with("user/me", max_retries=0)


@pytest.mark.parametrize("bad", [None, 42, ["sandbox"], {"server": "sandbox"}])
def test_normalize_server_rejects_non_strings_as_server_error(bad):
    with pytest.raises(ServerError, match="invalid server"):
        normalize_server(bad)


@pytest.mark.parametrize(
    "make",
    [
        lambda: make_client(server="x", username="u", password="p"),
        lambda: make_token_client(server="x"),
        lambda: make_static_token_client(server="x", token="t"),
    ],
)
def test_every_client_gets_the_get_only_read_retry_policy(make):
    client = make()
    # erclient's and requests' stock adapters are gone, not shadowed: exactly
    # one adapter per scheme, so nothing can outrank the policy by prefix
    assert sorted(client._http_session.adapters) == ["http://", "https://"]
    for scheme in ("https://x.pamdas.org/api/v1.0/subjects", "http://localhost:8000/api/v1.0/x"):
        retry = client._http_session.get_adapter(scheme).max_retries
        assert retry.total == 3
        assert retry.connect == 1  # a mistyped --server must not wait out 0/2/4 s
        assert set(retry.status_forcelist) == {429, 500, 502, 503, 504}
        assert retry.backoff_factor == 1
        assert retry.respect_retry_after_header is True
        assert retry.other == 0  # the one category allowed_methods does not gate
        assert retry.raise_on_status is False  # erclient maps the final status itself
        # writes are never replayed: a timed-out POST may have landed
        assert retry.is_retry("GET", 503) is True
        assert retry.is_retry("POST", 503) is False
        assert retry.is_retry("PATCH", 502) is False
        assert retry.is_retry("GET", 404) is False  # not transient


def test_read_retry_backoff_schedule():
    from urllib3.util.retry import RequestHistory

    from earthranger_cli.client import _read_retry_policy

    policy = _read_retry_policy()
    attempt = RequestHistory("GET", "/api/v1.0/subjects", None, 503, None)
    # urllib3: no wait before the first retry, then factor * 2**(n-1)
    waits = [policy.new(history=(attempt,) * n).get_backoff_time() for n in (1, 2, 3)]
    assert waits == [0, 2, 4]


def test_each_retry_is_announced_on_stderr(capsys):
    from urllib3.exceptions import ConnectTimeoutError
    from urllib3.response import HTTPResponse

    from earthranger_cli.client import _read_retry_policy

    policy = _read_retry_policy()
    once = policy.increment("GET", "/api/v1.0/subjects", response=HTTPResponse(status=503))
    twice = once.increment("GET", "/api/v1.0/subjects", error=ConnectTimeoutError())
    assert len(twice.history) == 2
    assert twice.__class__ is policy.__class__  # the note survives urllib3's cloning
    err = capsys.readouterr().err.splitlines()
    assert err == [
        "note: HTTP 503 on GET /api/v1.0/subjects; retrying (1 of 3)",
        "note: ConnectTimeoutError on GET /api/v1.0/subjects; retrying (2 of 3)",
    ]


def test_retry_after_is_capped():
    from urllib3.response import HTTPResponse

    from earthranger_cli.client import _read_retry_policy

    resp = HTTPResponse(status=503, headers={"Retry-After": "3600"})
    assert _read_retry_policy().get_retry_after(resp) == 30


@pytest.mark.parametrize("method", ["POST", "PATCH", "GET"])
def test_other_errors_never_retry_so_writes_cannot_be_replayed(method, capsys):
    from urllib3.exceptions import MaxRetryError, SSLError

    from earthranger_cli.client import _read_retry_policy

    # an SSL error after the request went out is urllib3's "other" category,
    # which allowed_methods does not gate: it must be exhausted immediately
    with pytest.raises(MaxRetryError):
        _read_retry_policy().increment(method, "/api/v1.0/activity/events", error=SSLError("boom"))
    assert capsys.readouterr().err == ""  # no retry happened, so no note


def test_first_choices_page_body_failure_is_retried(monkeypatch, capsys):
    from requests.exceptions import ChunkedEncodingError
    from urllib3.exceptions import ProtocolError

    from earthranger_cli import read

    monkeypatch.setattr(read, "_sleep", lambda s: None)
    client = Mock()
    client._get.side_effect = [
        ChunkedEncodingError(ProtocolError("Connection broken: IncompleteRead")),
        {"results": [{"value": "a"}], "next": None},
    ]
    assert [c["value"] for c in get_choices(client, "t1_species")] == ["a"]
    assert client._get.call_count == 2
    assert "retrying (1 of 3)" in capsys.readouterr().err


def test_get_me_body_failure_is_retried(monkeypatch):
    from requests.exceptions import ChunkedEncodingError
    from urllib3.exceptions import ProtocolError

    from earthranger_cli import read

    monkeypatch.setattr(read, "_sleep", lambda s: None)
    client = Mock()
    client._get.side_effect = [ChunkedEncodingError(ProtocolError("x")), {"username": "chris"}]
    assert get_me(client) == {"username": "chris"}
    assert client._get.call_count == 2


def test_collect_pages_refuses_a_looping_listing_but_not_a_long_one(monkeypatch):
    from erclient.er_errors import ERClientException

    from earthranger_cli import client as er
    from earthranger_cli import read

    class _Client:
        def __init__(self, responses):
            self.responses = responses

        def _api_root(self, version="v1.0"):
            return "https://x/api/v1.0"

        def _get(self, path, **kwargs):
            return self.responses[path.rsplit("/", 1)[-1]]

    # review: the authoring path must keep walking past the read surface's page cap
    monkeypatch.setattr(read, "MAX_PAGES", 1)
    long = _Client(
        {f"p{i}": {"results": [{"id": f"c{i}"}], "next": f"p{i + 1}"} for i in range(1, 4)}
    )
    long.responses["p4"] = {"results": [{"id": "c4"}], "next": None}
    first = {"count": 5, "next": "p1", "results": [{"id": "c0"}]}
    assert [r["id"] for r in er._collect_pages(long, first)] == ["c0", "c1", "c2", "c3", "c4"]
    # ...but a next link that repeats is still refused
    loop = _Client({"loop": {"results": [{"id": "c1"}], "next": "loop"}})
    with pytest.raises(ERClientException, match="repeated"):
        er._collect_pages(loop, {"count": 5, "next": "loop", "results": [{"id": "c0"}]})
