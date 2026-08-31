import pytest

from er_events_cli import config_store


def test_add_get_list_round_trip():
    config_store.add_profile("sandbox", server="sandbox", username="chris")
    config_store.add_profile("prod", server="https://myreserve.pamdas.org")
    assert config_store.get_profile("sandbox") == {"server": "sandbox", "username": "chris"}
    assert config_store.get_profile("prod") == {"server": "https://myreserve.pamdas.org"}
    assert sorted(config_store.list_profiles()) == ["prod", "sandbox"]
    assert config_store.get_profile("nope") is None


def test_first_profile_becomes_active():
    config_store.add_profile("sandbox", server="sandbox")
    assert config_store.active_profile() == ("sandbox", {"server": "sandbox"})
    config_store.add_profile("prod", server="prod")
    assert config_store.active_profile()[0] == "sandbox"  # later adds don't steal active


def test_set_active():
    config_store.add_profile("a", server="a")
    config_store.add_profile("b", server="b")
    config_store.set_active("b")
    assert config_store.active_profile()[0] == "b"
    with pytest.raises(config_store.ConfigError, match="no profile named 'zzz'"):
        config_store.set_active("zzz")


def test_remove_profile_clears_active():
    config_store.add_profile("a", server="a")
    assert config_store.remove_profile("a") is True
    assert config_store.active_profile() is None
    assert config_store.remove_profile("a") is False


def test_invalid_profile_name_rejected():
    for bad in ("../evil", "no spaces", "UPPER", ""):
        with pytest.raises(config_store.ConfigError):
            config_store.add_profile(bad, server="x")


def test_corrupt_config_is_a_miss():
    config_store.add_profile("a", server="a")
    config_store.config_file().write_text("{broken")
    assert config_store.list_profiles() == {}
    assert config_store.active_profile() is None
