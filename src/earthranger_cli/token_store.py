"""Per-profile OAuth token cache (gcloud-style).

Persists the tokens from `er auth login` so later invocations reuse them
instead of asking for a password. One file per PROFILE under
``<config>/tokens/<profile>.json`` (0600, directory 0700) — the profile is
the unit of identity, so its session travels with it and is invalidated when
its server or username changes. The config directory defaults to
``~/.config/er-events`` and can be overridden with ``ER_EVENTS_CONFIG_DIR``.
Passwords are never stored here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from .client import normalize_server
from .config_store import config_dir, write_private


def tokens_dir() -> Path:
    return config_dir() / "tokens"


def server_host(server: str) -> str:
    return urlparse(normalize_server(server)).netloc


def token_file(profile: str) -> Path:
    tokens = tokens_dir()
    path = tokens / f"{profile}.json"
    # A name with a path separator or ".." must not escape the tokens dir
    # (profile names are also validated at creation time).
    if path.resolve().parent != tokens.resolve():
        raise ValueError(f"invalid profile name: {profile!r}")
    return path


def save_token(profile: str, auth: dict, expires_at: datetime, username: str) -> None:
    path = token_file(profile)
    payload = {
        "access_token": auth["access_token"],
        "refresh_token": auth.get("refresh_token") or "",
        "token_type": auth.get("token_type") or "Bearer",
        "expires_at": expires_at.isoformat(),
        "username": username,
    }
    write_private(path, json.dumps(payload, indent=2))


def load_token(profile: str) -> dict | None:
    try:
        path = token_file(profile)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None  # corrupt cache == miss; caller falls back to other auth
    if not _is_valid_token_data(data):
        return None
    return data


def is_expired(data: dict) -> bool:
    return datetime.fromisoformat(data["expires_at"]) <= datetime.now(UTC)


def delete_token(profile: str) -> bool:
    """Delete the cached token; True iff a file was actually removed."""
    try:
        token_file(profile).unlink()
        return True
    except (OSError, ValueError):
        return False


def apply_to_client(client, data: dict) -> None:
    """Restore a cached token onto an ERClient so it skips password login.

    erclient's auth_headers() treats the token as live until auth_expires,
    then refreshes it with the cached refresh_token and client_id.
    """
    client.auth = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token") or "",
        "token_type": data.get("token_type") or "Bearer",
    }
    client.auth_expires = datetime.fromisoformat(data["expires_at"])


def _is_valid_token_data(data) -> bool:
    if not isinstance(data, dict) or not data.get("access_token"):
        return False
    expires_at = data.get("expires_at")
    if not isinstance(expires_at, str):
        return False
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    # Must be tz-aware; a naive timestamp would crash comparisons against
    # erclient's timezone-aware "now".
    return parsed.tzinfo is not None
