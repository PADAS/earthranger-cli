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


def test_active_round_trip():
    config_store.add_profile("a", server="a")
    assert config_store.get_active() is None
    config_store.set_active("a")
    assert config_store.get_active() == "a"
    config_store.set_active(None)
    assert config_store.get_active() is None


def test_set_active_unknown_profile_raises():
    with pytest.raises(config_store.ConfigError):
        config_store.set_active("nope")


def test_active_pointing_at_missing_profile_reads_as_none():
    import json

    config_store.add_profile("a", server="a")
    # a hand-edited or stale pointer must not surface as a selection
    f = config_store.config_file()
    data = json.loads(f.read_text())
    data["active"] = "gone"
    f.write_text(json.dumps(data))
    assert config_store.get_active() is None
    assert config_store.list_profiles() == {"a": {"server": "a"}}


def test_mutations_refuse_a_corrupt_config():
    # reads treat an unparseable file as empty; writes must never turn that
    # emptiness into the file's new contents
    config_store.add_profile("a", server="a")
    config_store.config_file().write_text("{broken")
    for mutate in (
        lambda: config_store.add_profile("b", server="b"),
        lambda: config_store.add_profile("b", server="b", default_if_new=True),
        lambda: config_store.set_active("a"),
        lambda: config_store.set_active(None),
        lambda: config_store.set_profile_property("a", "username", "u"),
        lambda: config_store.remove_profile("a"),
    ):
        with pytest.raises(config_store.ConfigError, match="not a valid config file"):
            mutate()
    assert config_store.config_file().read_text() == "{broken"


def test_clearing_active_removes_a_stale_pointer():
    import json

    config_store.add_profile("a", server="a")
    f = config_store.config_file()
    data = json.loads(f.read_text())
    data["active"] = "gone"
    f.write_text(json.dumps(data))
    config_store.set_active(None)
    assert "active" not in json.loads(f.read_text())


def test_add_profile_default_if_new_decides_under_the_lock():
    # the switch decision is made from the locked read, in the same write
    add = config_store.add_profile
    assert add("a", server="a", default_if_new=True) == (True, "a")
    assert add("b", server="b", default_if_new=True) == (True, "b")  # new: switches
    assert add("a", server="a2", default_if_new=True) == (False, "b")  # edit: stays
    assert add("c", server="c") == (False, "b")  # not asked: never switches
    config_store.set_active(None)
    assert add("a", server="a3", default_if_new=True) == (True, "a")  # no default yet
    assert config_store.get_profile("a") == {"server": "a3"}


def test_unreadable_config_dir_reads_as_empty(tmp_path, monkeypatch):
    import os

    d = tmp_path / "locked"
    d.mkdir()
    monkeypatch.setenv("ER_EVENTS_CONFIG_DIR", str(d))
    os.chmod(d, 0)
    try:
        assert config_store.list_profiles() == {}
        assert config_store.get_active() is None
    finally:
        os.chmod(d, 0o700)


def test_remove_profile_clears_active():
    config_store.add_profile("a", server="a")
    config_store.add_profile("b", server="b")
    config_store.set_active("a")
    config_store.remove_profile("b")
    assert config_store.get_active() == "a"  # removing another profile leaves it
    config_store.remove_profile("a")
    assert config_store.get_active() is None


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
