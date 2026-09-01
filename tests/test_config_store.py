import pytest

from er_events_cli import config_store


def test_add_get_list_round_trip():
    config_store.add_profile("sandbox", server="sandbox", username="chris")
    config_store.add_profile("prod", server="https://myreserve.pamdas.org")
    assert config_store.get_profile("sandbox") == {"server": "sandbox", "username": "chris"}
    assert config_store.get_profile("prod") == {"server": "https://myreserve.pamdas.org"}
    assert sorted(config_store.list_profiles()) == ["prod", "sandbox"]
    assert config_store.get_profile("nope") is None


def test_remove_profile():
    config_store.add_profile("a", server="a")
    assert config_store.remove_profile("a") is True
    assert config_store.remove_profile("a") is False


def test_legacy_active_key_is_ignored():
    import json

    config_store.add_profile("a", server="a")
    # old config files carried a global "active" pointer; it is now ignored
    f = config_store.config_file()
    data = json.loads(f.read_text())
    data["active"] = "a"
    f.write_text(json.dumps(data))
    assert config_store.list_profiles() == {"a": {"server": "a"}}


def test_invalid_profile_name_rejected():
    for bad in ("../evil", "no spaces", "UPPER", ""):
        with pytest.raises(config_store.ConfigError):
            config_store.add_profile(bad, server="x")


def test_corrupt_config_is_a_miss():
    config_store.add_profile("a", server="a")
    config_store.config_file().write_text("{broken")
    assert config_store.list_profiles() == {}
