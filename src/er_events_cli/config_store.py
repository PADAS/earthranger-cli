"""Named site profiles and the shared private config directory.

Profiles map a name to {server, username?} plus an ``active`` pointer, stored
in ``<config>/config.json`` — no secrets (tokens live in token_store). The
config directory defaults to ``~/.config/er-events`` and can be overridden
with ``ER_EVENTS_CONFIG_DIR``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
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


def _load() -> dict:
    path = config_file()
    if not path.exists():
        return {"profiles": {}, "active": None}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"profiles": {}, "active": None}  # corrupt config == empty
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        return {"profiles": {}, "active": None}
    return {"profiles": data["profiles"], "active": data.get("active")}


def _save(cfg: dict) -> None:
    write_private(config_file(), json.dumps(cfg, indent=2))


def add_profile(name: str, *, server: str, username: str | None = None) -> None:
    if not isinstance(name, str) or not _PROFILE_NAME_RE.match(name):
        raise ConfigError(
            f"invalid profile name {name!r} (use lowercase letters, digits, '-', '_')"
        )
    cfg = _load()
    profile: dict = {"server": server}
    if username:
        profile["username"] = username
    cfg["profiles"][name] = profile
    if cfg["active"] is None:
        cfg["active"] = name
    _save(cfg)


def get_profile(name: str) -> dict | None:
    return _load()["profiles"].get(name)


def list_profiles() -> dict:
    return _load()["profiles"]


def active_profile() -> tuple[str, dict] | None:
    cfg = _load()
    name = cfg.get("active")
    if name and name in cfg["profiles"]:
        return name, cfg["profiles"][name]
    return None


def set_active(name: str) -> None:
    cfg = _load()
    if name not in cfg["profiles"]:
        raise ConfigError(f"no profile named {name!r}; see 'er-events profile list'")
    cfg["active"] = name
    _save(cfg)


def remove_profile(name: str) -> bool:
    cfg = _load()
    if name not in cfg["profiles"]:
        return False
    del cfg["profiles"][name]
    if cfg["active"] == name:
        cfg["active"] = None
    _save(cfg)
    return True
