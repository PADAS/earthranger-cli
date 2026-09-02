import json
import os
import stat
from datetime import UTC, datetime, timedelta

import pytest

from earthranger_cli import token_store

EXPIRES = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)
AUTH = {
    "access_token": "acc-123",
    "refresh_token": "ref-456",
    "token_type": "Bearer",
    "expires_in": 3600,
}


def test_server_host_from_bare_name_and_url():
    assert token_store.server_host("myreserve") == "myreserve.pamdas.org"
    assert token_store.server_host("https://sandbox.pamdas.org/") == "sandbox.pamdas.org"


def test_save_and_load_round_trip():
    token_store.save_token("sandbox.pamdas.org", AUTH, EXPIRES, "chris")
    data = token_store.load_token("sandbox.pamdas.org")
    assert data["access_token"] == "acc-123"
    assert data["refresh_token"] == "ref-456"
    assert data["token_type"] == "Bearer"
    assert data["expires_at"] == EXPIRES.isoformat()
    assert data["username"] == "chris"


def test_token_file_is_private():
    token_store.save_token("sandbox.pamdas.org", AUTH, EXPIRES, "chris")
    path = token_store.token_file("sandbox.pamdas.org")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_load_missing_returns_none():
    assert token_store.load_token("nowhere.pamdas.org") is None


def test_load_corrupt_json_returns_none():
    token_store.save_token("s.pamdas.org", AUTH, EXPIRES, "u")
    token_store.token_file("s.pamdas.org").write_text("{not json")
    assert token_store.load_token("s.pamdas.org") is None


def test_load_missing_fields_or_naive_timestamp_returns_none():
    path_host = "s.pamdas.org"
    token_store.save_token(path_host, AUTH, EXPIRES, "u")
    f = token_store.token_file(path_host)
    f.write_text(json.dumps({"refresh_token": "r", "expires_at": EXPIRES.isoformat()}))
    assert token_store.load_token(path_host) is None
    f.write_text(json.dumps({"access_token": "a", "expires_at": "2026-09-30T12:00:00"}))
    assert token_store.load_token(path_host) is None


def test_token_file_rejects_path_escape():
    with pytest.raises(ValueError):
        token_store.token_file("../evil")


def test_delete_token():
    token_store.save_token("s.pamdas.org", AUTH, EXPIRES, "u")
    assert token_store.delete_token("s.pamdas.org") is True
    assert token_store.delete_token("s.pamdas.org") is False


def test_apply_to_client_restores_auth():
    from conftest import FakeER

    token_store.save_token("s.pamdas.org", AUTH, EXPIRES, "u")
    data = token_store.load_token("s.pamdas.org")
    client = FakeER()
    token_store.apply_to_client(client, data)
    assert client.auth == {
        "access_token": "acc-123",
        "refresh_token": "ref-456",
        "token_type": "Bearer",
    }
    assert client.auth_expires == EXPIRES


def test_is_expired():
    fresh = {"expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    stale = {"expires_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}
    assert token_store.is_expired(fresh) is False
    assert token_store.is_expired(stale) is True


def test_profile_lock_is_exclusive(tmp_path, monkeypatch):
    # G2+G5/r3910752062 — session mutations serialize on a per-profile flock
    import fcntl

    import pytest

    monkeypatch.setenv("ER_EVENTS_CONFIG_DIR", str(tmp_path))
    with token_store.profile_lock("dev"):
        lock_path = token_store.token_file("dev").parent / "dev.json.lock"
        assert lock_path.exists()
        with open(lock_path) as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # released on exit
    with open(lock_path) as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(other, fcntl.LOCK_UN)
