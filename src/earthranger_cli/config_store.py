"""Named site profiles and the shared private config directory.

Profiles map a name to {server, username?}, stored in ``<config>/config.json``
— no secrets (tokens live in token_store). Profile selection is per invocation
(--profile flag or ER_PROFILE env var); there is no global active pointer. The
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


def remove_profile(name: str) -> bool:
    cfg = _load()
    if name not in cfg["profiles"]:
        return False
    del cfg["profiles"][name]
    _save(cfg)
    return True
