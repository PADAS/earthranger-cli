import pytest

from earthranger_cli import config_store


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


def test_config_lock_is_exclusive(tmp_path, monkeypatch):
    # J2/r3915824004 — config.json mutations serialize on a process-wide flock
    import fcntl

    monkeypatch.setenv("ER_EVENTS_CONFIG_DIR", str(tmp_path))
    with config_store._config_lock():
        lock_path = tmp_path / "config.json.lock"
        assert lock_path.exists()
        with open(lock_path) as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_set_profile_property_reads_under_config_lock(monkeypatch):
    # J2/r3915824004 — the read-modify-write must see writes committed by
    # whoever held the lock just before us, not a pre-lock snapshot
    from contextlib import contextmanager

    config_store.add_profile("a", server="s1", username="u1")
    config_store.add_profile("b", server="s2", username="u2")
    real_lock = config_store._config_lock

    @contextmanager
    def racing_lock():
        with real_lock():
            # another process changed profile b just before we acquired it
            cfg = config_store._load()
            cfg["profiles"]["b"]["username"] = "changed-by-other"
            config_store._save(cfg)
            yield

    monkeypatch.setattr(config_store, "_config_lock", racing_lock)
    config_store.set_profile_property("a", "username", "u1-new")
    assert config_store.get_profile("a")["username"] == "u1-new"
    assert config_store.get_profile("b")["username"] == "changed-by-other"
