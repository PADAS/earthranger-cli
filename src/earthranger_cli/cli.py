"""Click CLI wiring. Logic lives in the other modules; this file only parses
flags, connects, and formats output."""

from __future__ import annotations

import functools
import json
import sys

import click
import requests.exceptions
from erclient.er_errors import ERClientBadCredentials, ERClientException

from . import client as er
from . import config_store, token_store
from .apply import ApplyError, apply_spec, extract_choice_fields, normalize_v2_schema
from .choices import choice_sort_key
from .client import make_client, make_static_token_client, make_token_client, normalize_server
from .dsl import SpecError, load_spec
from .events import FieldArgError, build_event, load_events_file, parse_field_args, post_events
from .pull import PullError, pull_category, render_spec_yaml


def _api_errors(f):
    """Turn API failures (auth, writes) into a clean message and exit 1."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except ERClientBadCredentials as e:
            click.echo(f"error: {_bad_credentials_message(e)}")
            sys.exit(1)
        except (
            ApplyError,
            PullError,
            config_store.ConfigError,
            ERClientException,
            requests.exceptions.RequestException,
        ) as e:
            click.echo(f"error: {e}")
            sys.exit(1)

    return wrapper


def _bad_credentials_message(e: ERClientBadCredentials) -> str:
    """A 401 mid-command: say which credential the server refused and how to fix it.

    A static token can't be checked up front (erclient treats it as valid
    until 2099), so this is where a revoked or expired one first shows up.
    """
    ctx = click.get_current_context(silent=True)
    obj = (ctx.obj if ctx is not None else None) or {}
    if obj.get("token"):
        return f"credentials rejected ({e}) — check --token / ER_TOKEN."
    if obj.get("password"):
        return f"credentials rejected ({e}) — check --username / --password."
    name = obj.get("profile")
    if name:
        return (
            f"credentials rejected ({e}) — the credential stored on profile {name!r} is "
            "expired or revoked; run 'er auth login' (or 'er auth login --token')."
        )
    return f"credentials rejected ({e})."


def connection_options(f):
    """Accept the connection flags on a leaf command too (people naturally type
    them after the subcommand); provided values override the root group's."""

    def wrapper(
        *args,
        server_=None,
        username_=None,
        password_=None,
        token_=None,
        profile_=None,
        **kwargs,
    ):
        ctx = click.get_current_context()
        for key, val in (
            ("server", server_),
            ("username", username_),
            ("password", password_),
            ("token", token_),
            ("profile", profile_),
        ):
            if val:
                ctx.obj[key] = val
        return f(*args, **kwargs)

    wrapper = functools.update_wrapper(wrapper, f)
    for opt in (
        click.option("--profile", "profile_", help="Named profile to use."),
        click.option("--token", "token_", help="Pre-issued OAuth bearer token."),
        click.option("--password", "password_", help="EarthRanger password."),
        click.option("--username", "username_", help="EarthRanger username."),
        click.option("--server", "server_", help="ER site name or full https:// URL."),
    ):
        wrapper = opt(wrapper)
    return wrapper


def _resolve_connection(ctx) -> tuple[str, str | None]:
    """Resolve (server, username) from flags/env or a selected profile.
    Explicit --server/--username always win; a profile named with --profile
    (or ER_PROFILE, e.g. via the er-use shell helper) supplies the defaults.
    Profile selection is per invocation — there is no global active profile."""
    server = ctx.obj["server"]
    username = ctx.obj["username"]
    profile = None
    name = ctx.obj.get("profile")
    if name:
        profile = config_store.get_profile(name)
        if profile is None:
            raise click.UsageError(f"Unknown profile {name!r}. See 'er profile list'.")
    if profile:
        server = server or profile.get("server")
        username = username or profile.get("username")
    if not server:
        raise click.UsageError(
            "Missing server: pass --server, set ER_SERVER, or select a profile "
            "(--profile NAME / ER_PROFILE)."
        )
    return server, username


def _connect(ctx):
    """Build an authenticated client.

    Precedence: an explicit bearer token (--token or ER_TOKEN) wins; else an
    explicit password (flag or ER_PASSWORD); else the selected profile's
    stored record (a static token from `auth login --token` or a session
    cached by `auth login`); else an interactive password prompt.
    """
    server, username = _resolve_connection(ctx)
    token = ctx.obj.get("token")
    if token:
        # the token is the identity: no username, no cache, no refresh
        return make_static_token_client(server=server, token=token)
    password = ctx.obj["password"]
    name = ctx.obj.get("profile")
    if not password and name:
        profile, cached = _profile_session_snapshot(name)
        # The session is bound to the profile's server: a --server/ER_SERVER
        # override must never receive the profile's token — compared as full
        # normalized URLs, so an http:// downgrade or a different base path on
        # the same host also misses. Likewise an explicit --username that
        # isn't the session's owner. The profile+token pair is read under the
        # profile lock so a concurrent repoint+re-login can't pair the new
        # session with the previously resolved server.
        if profile is not None and cached:
            server_matches = normalize_server(server) == normalize_server(profile["server"])
            if server_matches and (not username or cached.get("username") == username):
                return _connect_with_cached_token(ctx, name, profile, server, cached)
    if not username:
        raise click.UsageError("Missing username: pass --username or set ER_USERNAME.")
    if not password:
        password = click.prompt("Password", hide_input=True)
    return make_client(server=server, username=username, password=password)


def _profile_session_snapshot(name: str) -> tuple[dict | None, dict | None]:
    """One coherent (profile, token) pair, read together under the profile
    lock — an unlocked pair could mix identities during a concurrent
    'profile set server' + 'auth login'."""
    with token_store.profile_lock(name):
        return config_store.get_profile(name), token_store.load_token(name)


def _connect_with_cached_token(ctx, name: str, profile: dict, server: str, cached: dict):
    client = make_token_client(server=server)
    token_store.apply_to_client(client, cached)
    if token_store.is_static(cached):
        # nothing to refresh or rotate; a revoked static token can only be
        # learned from the first real request (reported by _api_errors)
        return client
    try:
        # Eager: refreshes an expired access token now (via the refresh token),
        # so a dead session fails here with a clear message instead of mid-command.
        client.auth_headers()
    except ERClientException as e:
        raise ERClientException(
            f"cached session for profile {name!r} expired or invalid — run 'er auth login'"
        ) from e
    profile_snapshot = dict(profile)

    def _persist_rotation():
        auth = getattr(client, "auth", None) or {}
        if not auth.get("access_token") or auth["access_token"] == cached["access_token"]:
            return
        # compare-and-swap under the profile lock: another process may have
        # logged out, re-logged-in, or repointed the profile mid-command —
        # never resurrect that session. This runs from call_on_close, after
        # _api_errors has returned and the command has succeeded — persistence
        # is best-effort, so storage failures warn instead of raising.
        try:
            with token_store.profile_lock(name):
                current = token_store.load_token(name)
                if not current or current.get("access_token") != cached["access_token"]:
                    return
                if config_store.get_profile(name) != profile_snapshot:
                    return
                token_store.save_token(
                    name, auth, client.auth_expires, cached.get("username") or ""
                )
        except (config_store.ConfigError, OSError) as e:
            click.echo(
                f"warning: could not persist rotated session for profile {name!r}: {e} "
                "— the next command may need to refresh again.",
                err=True,
            )

    ctx.call_on_close(_persist_rotation)
    return client


@click.group()
@click.option("--server", envvar="ER_SERVER", help="ER site name (myreserve) or full https:// URL.")
@click.option("--username", envvar="ER_USERNAME", help="EarthRanger username.")
@click.option(
    "--password", envvar="ER_PASSWORD", help="EarthRanger password (prompted if omitted)."
)
@click.option(
    "--token",
    envvar="ER_TOKEN",
    help="Pre-issued OAuth bearer token (wins over --password and cached sessions).",
)
@click.option("--profile", envvar="ER_PROFILE", help="Named profile to use (see 'er profile').")
@click.pass_context
def main(ctx, server, username, password, token, profile):
    """EarthRanger site management CLI."""
    ctx.obj = {
        "server": server,
        "username": username,
        "password": password,
        "token": token,
        "profile": profile,
    }


@main.group("events")
def events_group():
    """Create and edit event categories, choices, and v2 event types; post events."""


@events_group.command("apply")
@connection_options
@click.argument("spec_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--dry-run", is_flag=True, help="Show planned changes without writing.")
@click.pass_context
@_api_errors
def apply_cmd(ctx, spec_file, dry_run):
    """Create or update the category, choices, and event types in SPEC_FILE."""
    try:
        spec = load_spec(spec_file)
    except SpecError as e:
        for err in e.errors:
            click.echo(f"error: {err}")
        sys.exit(1)
    client = _connect(ctx)
    for r in apply_spec(client, spec, dry_run=dry_run):
        action = r.action
        if dry_run and action != "unchanged":
            action = f"would-{action}"
        line = f"{r.kind:<11} {action:<12} {r.name}"
        if r.detail:
            line += f"  ({r.detail})"
        click.echo(line)


@events_group.command("post")
@connection_options
@click.option("--event-type", "event_type", help="Event type value (required unless --file).")
@click.option(
    "--field", "fields", multiple=True, help="key=value; value parsed as a YAML scalar. Repeatable."
)
@click.option("--location", help="LAT,LON")
@click.option("--time", "time_", help="ISO-8601 timestamp; defaults to now (UTC).")
@click.option("--title", help="Event title.")
@click.option(
    "--file",
    "file_",
    type=click.Path(exists=True, dir_okay=False),
    help="YAML list of events to post.",
)
@click.pass_context
@_api_errors
def post_event_cmd(ctx, event_type, fields, location, time_, title, file_):
    """Post one event (via flags) or a batch (via --file)."""
    try:
        if file_:
            events = load_events_file(file_)
        elif event_type:
            events = [
                build_event(
                    event_type=event_type,
                    details=parse_field_args(list(fields)),
                    location=location,
                    time=time_,
                    title=title,
                )
            ]
        else:
            raise click.UsageError("Pass --event-type (with --field ...) or --file.")
    except FieldArgError as e:
        click.echo(f"error: {e}")
        sys.exit(1)
    client = _connect(ctx)
    failures = 0
    for event, error in zip(events, post_events(client, events)):
        if error is None:
            click.echo(f"posted   {event['event_type']}")
        else:
            failures += 1
            click.echo(f"FAILED   {event['event_type']}: {error}")
    if failures:
        sys.exit(1)


@events_group.group("list")
def list_group():
    """List objects on the server."""


@list_group.command("categories")
@connection_options
@click.pass_context
@_api_errors
def list_categories(ctx):
    """List event categories (inactive included)."""
    client = _connect(ctx)
    for c in client.get_event_categories(include_inactive=True):
        active = "" if c.get("is_active", True) else "  (inactive)"
        click.echo(f"{c.get('value'):<40} {c.get('display')}{active}")


def _category_value_of(event_type: dict):
    raw = event_type.get("category")
    return raw.get("value") if isinstance(raw, dict) else raw


@list_group.command("event-types")
@connection_options
@click.option("--category", help="Filter by category value.")
@click.pass_context
@_api_errors
def list_event_types(ctx, category):
    """List event types (inactive included)."""
    client = _connect(ctx)
    for t in client.get_event_types(include_inactive=True, version="v2.0"):
        cat_value = _category_value_of(t)
        if category and cat_value != category:
            continue
        active = "" if t.get("is_active", True) else "  (inactive)"
        click.echo(f"{t.get('value'):<40} {t.get('display'):<40} {cat_value}{active}")


@events_group.group("show")
def show_group():
    """Show one object in full."""


@show_group.command("event-type")
@connection_options
@click.argument("value")
@click.pass_context
@_api_errors
def show_event_type(ctx, value):
    """Print the full v2 event type JSON plus its referenced Choice records."""
    client = _connect(ctx)
    types = client.get_event_types(include_inactive=True, include_schema=True, version="v2.0")
    et = next((t for t in types if t.get("value") == value), None)
    if et is None:
        click.echo(f"error: no event type with value {value!r}")
        sys.exit(1)
    fields = extract_choice_fields(normalize_v2_schema(et.get("schema") or {}))
    choices = {f: er.get_choices(client, f) for f in fields}
    click.echo(json.dumps({"event_type": et, "choices": choices}, indent=2))


@main.group("auth")
def auth_group():
    """Authenticate and manage cached tokens."""


@auth_group.command("login")
@connection_options
@click.pass_context
@_api_errors
def auth_login(ctx):
    """Log in (password, or --token) and cache the credential on the selected profile."""
    name = _require_selected_profile(ctx)
    server, username = _resolve_connection(ctx)
    profile = config_store.get_profile(name) or {}
    if normalize_server(server) != normalize_server(profile.get("server") or ""):
        raise click.UsageError(
            f"server {normalize_server(server)!r} differs from profile {name!r} "
            f"({normalize_server(profile.get('server') or '')}); update it first "
            "with 'er profile set server ...'."
        )
    host = token_store.server_host(server)
    token = ctx.obj.get("token")
    if token:
        # a pre-issued bearer token: verify it against /user/me/ so a typo
        # fails here rather than on the first real command, and learn the
        # owner so the profile's identity and the username-match rule work
        client = make_static_token_client(server=server, token=token)
        try:
            owner = client.get_me()["username"]
        except ERClientBadCredentials as e:
            # only a 401 means the token is bad; outages, 404s and 403s
            # propagate to _api_errors with their own message
            click.echo(f"error: token rejected by {host}: {e}")
            sys.exit(1)
        except (KeyError, TypeError):
            click.echo(f"error: unexpected /user/me/ response from {host}; token not stored.")
            sys.exit(1)
        explicit = ctx.obj.get("username")
        if explicit and explicit != owner:
            # same rule as _connect: never let an explicit identity ride on
            # someone else's credential
            raise click.UsageError(f"token belongs to {owner!r}, not --username {explicit!r}.")
        username = owner
        # erclient seeded these from the token; storing them (rather than a
        # copy) keeps the record identical to what just passed /user/me/
        auth = client.auth
        expires_at = client.auth_expires
        static = True
    else:
        password = ctx.obj["password"]
        if not username:
            raise click.UsageError("Missing username: pass --username or set ER_USERNAME.")
        if not password:
            password = click.prompt("Password", hide_input=True)
        client = make_client(server=server, username=username, password=password)
        if not client.login():
            click.echo(f"error: login failed for {username!r} at {host}")
            sys.exit(1)
        auth = client.auth
        expires_at = client.auth_expires
        static = False
    with token_store.profile_lock(name):
        # the token was minted for the profile as it stood before the network
        # round-trip; if a concurrent command repointed it since, this session
        # belongs to the old identity and must not be cached
        if config_store.get_profile(name) != profile:
            click.echo(
                f"error: profile {name!r} changed during login — session not cached; "
                "re-run 'er auth login'."
            )
            sys.exit(1)
        token_store.save_token(name, auth, expires_at, username, static=static)
        if profile.get("username") != username:
            # the profile's identity follows whoever actually logged in
            config_store.set_profile_property(name, "username", username)
            click.echo(f"Profile {name!r} username set to {username!r}.")
    if static:
        click.echo(
            f"Authenticated with a static token. Stored on profile {name!r} ({host}); "
            "it will not be refreshed — re-run 'er auth login --token' when it expires."
        )
    else:
        click.echo(f"Authenticated. Session cached on profile {name!r} ({host}).")


def _require_selected_profile(ctx) -> str:
    name = ctx.obj.get("profile")
    if not name:
        raise click.UsageError(
            "a selected profile is required — create one with 'er profile add' "
            "(it auto-switches) or select one with 'er profile use'."
        )
    if config_store.get_profile(name) is None:
        raise config_store.ConfigError(f"no profile named {name!r}")
    return name


@auth_group.command("logout")
@connection_options
@click.pass_context
@_api_errors
def auth_logout(ctx):
    """Delete the selected profile's cached session."""
    name = _require_selected_profile(ctx)
    with token_store.profile_lock(name):
        removed = token_store.delete_token(name)
    click.echo("Logged out." if removed else "No cached session.")


@auth_group.command("status")
@connection_options
@click.pass_context
@_api_errors
def auth_status(ctx):
    """Report the selected profile's cached session and its expiry."""
    name = _require_selected_profile(ctx)
    profile, data = _profile_session_snapshot(name)
    profile = profile or {}
    host = token_store.server_host(profile.get("server") or "")
    click.echo(f"{name} ({host}): {_auth_detail(data)}")


@events_group.command("pull")
@connection_options
@click.argument("category_value")
@click.option("-o", "--output", type=click.Path(dir_okay=False), help="Write the spec to a file.")
@click.option(
    "--skip-unsupported",
    is_flag=True,
    help="Write anyway, dropping constructs the DSL cannot express.",
)
@click.pass_context
@_api_errors
def pull_cmd(ctx, category_value, output, skip_unsupported):
    """Reconstruct a DSL spec from the server's CATEGORY_VALUE (reverse of apply)."""
    client = _connect(ctx)
    result = pull_category(client, category_value)
    for w in result.unsupported:
        click.echo(f"warning: {w}")
    if result.unsupported and not skip_unsupported:
        click.echo(
            "error: the server contains constructs the DSL cannot express; "
            "re-run with --skip-unsupported to drop them"
        )
        sys.exit(1)
    text = render_spec_yaml(result)
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(text)
        click.echo(f"Wrote {output}")
    else:
        click.echo(text, nl=False)


def _auth_state(data: dict | None) -> str:
    if not data:
        return "not authenticated"
    if token_store.is_static(data):
        return "static token"
    return "expired" if token_store.is_expired(data) else "valid"


def _auth_detail(data: dict | None) -> str:
    """Long form for `auth status` / `profile show`: state, owner, expiry."""
    if not data:
        return "not authenticated"
    as_user = f" as {data['username']}" if data.get("username") else ""
    if token_store.is_static(data):
        return f"static token{as_user} (never refreshes)"
    return f"{_auth_state(data)}{as_user} (access token expires {data['expires_at']})"


@main.group("profile")
def profile_group():
    """Manage named site profiles."""


@profile_group.command("add")
@click.argument("name")
@click.option("--server", "p_server", required=True, help="ER site name or full https:// URL.")
@click.option("--username", "p_username", help="Default username for this profile.")
@_api_errors
def profile_add(name, p_server, p_username):
    """Add (or overwrite) a profile."""
    with token_store.profile_lock(name):
        existing = config_store.get_profile(name)
        if existing is None:
            # no profile in config (never created, or config lost/corrupt) —
            # any surviving token file is an orphan from an unknown identity
            # and must not be adopted by the profile being created
            token_store.delete_token(name)
        if existing is not None:
            # identity is decided from the profiles alone: an existing token
            # file that merely fails to parse right now must still be cleared
            same_identity = (
                normalize_server(existing.get("server") or "") == (normalize_server(p_server))
                and existing.get("username") == p_username
            )
            if not same_identity and token_store.delete_token(name):
                click.echo(
                    f"Cleared cached session for profile {name!r} (identity changed); "
                    "run 'er auth login'.",
                    err=True,
                )
        config_store.add_profile(name, server=p_server, username=p_username)
    # confirmation to stderr; stdout stays eval-able so the shell wrapper can
    # auto-switch this shell to the new profile (same pattern as profile use)
    click.echo(f"Added profile {name!r} ({token_store.server_host(p_server)}).", err=True)
    click.echo(f"export ER_PROFILE={name}")


@profile_group.command("list")
@connection_options
@click.pass_context
def profile_list(ctx):
    """List profiles: selection marker (--profile/ER_PROFILE), server, username, auth state."""
    profiles = config_store.list_profiles()
    if not profiles:
        click.echo("No profiles. Add one with 'er profile add NAME --server ...'.")
        return
    active_name = ctx.obj.get("profile")
    for name in sorted(profiles):
        p, data = _profile_session_snapshot(name)
        if p is None:
            continue  # removed since the listing was read
        marker = "*" if name == active_name else " "
        username = p.get("username") or "-"
        host = token_store.server_host(p.get("server") or "")
        click.echo(f"{marker} {name:<16} {host:<40} {username:<16} {_auth_state(data)}")


@profile_group.command("remove")
@click.argument("name")
@_api_errors
def profile_remove(name):
    """Delete a profile and its cached session."""
    if config_store.get_profile(name) is None:
        raise config_store.ConfigError(f"no profile named {name!r}")
    with token_store.profile_lock(name):
        token_store.delete_token(name)  # raises on real failure, before removal
        config_store.remove_profile(name)
    click.echo(f"Removed profile {name!r}.")


@profile_group.command("current")
@connection_options
@click.pass_context
def profile_current(ctx):
    """Print this invocation's selected profile (--profile/ER_PROFILE); exit 1 if none."""
    name = ctx.obj.get("profile")
    if not name:
        sys.exit(1)
    click.echo(name)


@profile_group.command("use")
@click.argument("name", required=False)
@_api_errors
def profile_use(name):
    """Select a profile for the current shell.

    A subprocess cannot modify its parent shell's environment, so this prints
    the `export ER_PROFILE=...` line (or `unset` with no NAME) for the shell
    to eval — the `er` wrapper function from the README does that for you.
    """
    if name is None:
        click.echo("unset ER_PROFILE")
        return
    if config_store.get_profile(name) is None:
        raise config_store.ConfigError(f"no profile named {name!r}")
    click.echo(f"export ER_PROFILE={name}")


@profile_group.command("set")
@click.argument("key")
@click.argument("value")
@connection_options
@click.pass_context
@_api_errors
def profile_set(ctx, key, value):
    """Set a property (server, username) on the selected profile."""
    name = ctx.obj.get("profile")
    if not name:
        raise click.UsageError(
            "no profile selected — select one with 'er profile use' or pass --profile."
        )
    # the session is bound to the profile's identity: changing the server, or
    # the username to someone other than the session's owner, invalidates it —
    # including when the cached owner can't be verified (unreadable token
    # file). Delete BEFORE committing the change, under the profile lock, so a
    # failed cleanup never leaves a stale token attached to the new identity.
    with token_store.profile_lock(name):
        if key == "server":
            invalidates = True
        else:
            cached = token_store.load_token(name)
            invalidates = key == "username" and (cached is None or cached.get("username") != value)
        removed = token_store.delete_token(name) if invalidates else False
        config_store.set_profile_property(name, key, value)
    click.echo(f"Set {key} for profile {name!r}.")
    if removed:
        click.echo(f"Cleared cached session for profile {name!r}; run 'er auth login'.")


@profile_group.command("show")
@connection_options
@click.argument("name", required=False)
@click.pass_context
@_api_errors
def profile_show(ctx, name):
    """Show one profile in full (defaults to this shell's selection)."""
    if name is None:
        name = ctx.obj.get("profile")
        if not name:
            raise click.UsageError(
                "no profile selected — pass a NAME or select one with 'er profile use'."
            )
    profile, data = _profile_session_snapshot(name)
    if profile is None:
        raise config_store.ConfigError(f"no profile named {name!r}")
    server = profile["server"]
    host = token_store.server_host(server)
    click.echo(f"name:      {name}")
    click.echo(f"server:    {server}")
    click.echo(f"host:      {host}")
    click.echo(f"username:  {profile.get('username') or '-'}")
    click.echo(f"auth:      {_auth_detail(data)}")


@main.group("choices")
def choices_group():
    """Inspect Choice sets (writes are spec-driven via 'er events apply')."""


@choices_group.command("list")
@connection_options
@click.pass_context
@_api_errors
def choices_list(ctx):
    """List choice fields with option counts; flags sets with no v2 schema references."""
    client = _connect(ctx)
    by_field: dict = {}
    for r in er.get_all_choices(client):
        by_field.setdefault(r.get("field") or "", []).append(r)
    referenced: set = set()
    for t in client.get_event_types(include_inactive=True, include_schema=True, version="v2.0"):
        schema = normalize_v2_schema(t.get("schema") or {})
        if isinstance(schema, dict):
            referenced.update(extract_choice_fields(schema))
    for name in sorted(by_field):
        recs = by_field[name]
        active = sum(1 for r in recs if r.get("is_active", True))
        inactive = len(recs) - active
        orphan = "" if name in referenced else "  (unreferenced)"
        click.echo(f"{name:<42} {active:>3} active {inactive:>3} inactive{orphan}")


@choices_group.command("show")
@click.argument("field_name")
@connection_options
@click.pass_context
@_api_errors
def choices_show(ctx, field_name):
    """Print one choice set's records in display order."""
    client = _connect(ctx)
    records = er.get_choices(client, field_name)
    if not records:
        click.echo(f"error: no choices found for field {field_name!r}")
        sys.exit(1)
    for r in sorted(records, key=choice_sort_key):
        active = "" if r.get("is_active", True) else "  (inactive)"
        icon = f"  icon={r['icon']}" if r.get("icon") else ""
        click.echo(f"{r.get('value'):<40} {r.get('display')}{icon}{active}")
