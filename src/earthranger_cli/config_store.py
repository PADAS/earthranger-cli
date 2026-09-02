"""Named site profiles and the shared private config directory.

Profiles map a name to {server, username?}, stored in ``<config>/config.json``
— no secrets (tokens live in token_store). Profile selection is per invocation
(--profile flag or ER_PROFILE env var); there is no global active pointer. The
config directory defaults to ``~/.config/er-events`` and can be overridden
with ``ER_EVENTS_CONFIG_DIR``.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ConfigError(Exception):
    pass


def config_dir() -> Path:
    override = os.environ.get("ER_EVENTS_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path("~/.config/er-events").expanduser()


def config_file() -> Path:
    return config_dir() / "config.json"


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent)  # mkstemp creates 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def _config_lock():
    """Serialize read-modify-write of the shared config.json across processes.

    Per-profile session locks (token_store.profile_lock) don't cover this:
    two profiles' mutations race on the one file. Lock order is always
    profile_lock (outer, taken by the CLI) then this (inner, taken here);
    nothing under this lock ever takes a profile lock, so no deadlock.
    """
    path = config_dir() / "config.json.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        f = open(path, "w")  # noqa: SIM115 — enters `with f:` below; open split out so only acquisition converts to ConfigError
    except OSError as e:
        raise ConfigError(f"could not acquire config lock: {e}") from e
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
        except OSError as e:
            raise ConfigError(f"could not acquire config lock: {e}") from e
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _load() -> dict:
    path = config_file()
    if not path.exists():
        return {"profiles": {}}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"profiles": {}}  # corrupt config == empty
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        return {"profiles": {}}
    # legacy files may carry an "active" pointer; selection is per-shell now
    return {"profiles": data["profiles"]}


def _save(cfg: dict) -> None:
    write_private(config_file(), json.dumps(cfg, indent=2))


def add_profile(name: str, *, server: str, username: str | None = None) -> None:
    if not isinstance(name, str) or not _PROFILE_NAME_RE.match(name):
        raise ConfigError(
            f"invalid profile name {name!r} (use lowercase letters, digits, '-', '_')"
        )
    with _config_lock():
        cfg = _load()
        profile: dict = {"server": server}
        if username:
            profile["username"] = username
        cfg["profiles"][name] = profile
        _save(cfg)


def get_profile(name: str) -> dict | None:
    return _load()["profiles"].get(name)


def list_profiles() -> dict:
    return _load()["profiles"]


PROFILE_KEYS = ("server", "username")


def set_profile_property(name: str, key: str, value: str) -> None:
    if key not in PROFILE_KEYS:
        raise ConfigError(f"unknown profile property {key!r} (settable: {', '.join(PROFILE_KEYS)})")
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key}: a non-empty value is required")
    with _config_lock():
        cfg = _load()
        if name not in cfg["profiles"]:
            raise ConfigError(f"no profile named {name!r}")
        cfg["profiles"][name][key] = value
        _save(cfg)


def remove_profile(name: str) -> bool:
    with _config_lock():
        cfg = _load()
        if name not in cfg["profiles"]:
            return False
        del cfg["profiles"][name]
        _save(cfg)
        return True
