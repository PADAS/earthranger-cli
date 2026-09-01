from unittest.mock import Mock

import pytest
from erclient.er_errors import ERClientException

from earthranger_cli.client import (
    get_choices,
    make_client,
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
