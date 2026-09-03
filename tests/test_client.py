from unittest.mock import Mock

import pytest
from erclient.er_errors import ERClientException

from earthranger_cli.client import (
    ServerError,
    get_choices,
    get_me,
    make_client,
    make_static_token_client,
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


def test_get_me_is_a_one_shot_probe():
    client = Mock()
    client._get.return_value = {"username": "chris"}
    assert get_me(client) == {"username": "chris"}
    client._get.assert_called_once_with("user/me", max_retries=0)


@pytest.mark.parametrize("bad", [None, 42, ["sandbox"], {"server": "sandbox"}])
def test_normalize_server_rejects_non_strings_as_server_error(bad):
    with pytest.raises(ServerError, match="invalid server"):
        normalize_server(bad)
