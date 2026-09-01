"""Click CLI wiring. Logic lives in the other modules; this file only parses
flags, connects, and formats output."""

from __future__ import annotations

import functools
import json
import sys

import click
import requests.exceptions
from erclient.er_errors import ERClientException

from . import client as er
from . import config_store, token_store
from .apply import ApplyError, apply_spec, extract_choice_fields, normalize_v2_schema
from .client import make_client, make_token_client
from .dsl import SpecError, load_spec
from .events import FieldArgError, build_event, load_events_file, parse_field_args, post_events
from .pull import PullError, pull_category, render_spec_yaml


def _api_errors(f):
    """Turn API failures (auth, writes) into a clean message and exit 1."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
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

    Precedence: an explicit password (flag or ER_PASSWORD) wins; else a token
    cached by `er auth login`; else an interactive password prompt.
    """
    server, username = _resolve_connection(ctx)
    password = ctx.obj["password"]
    if not password:
        cached = token_store.load_token(token_store.server_host(server))
        # An explicit username (flag/env/profile) that doesn't match the
        # cached session's owner must not silently ride on someone else's
        # cache; no explicit username, or a matching one, keeps using it.
        if cached and (not username or cached.get("username") == username):
            return _connect_with_cached_token(ctx, server, cached)
    if not username:
        raise click.UsageError("Missing username: pass --username or set ER_USERNAME.")
    if not password:
        password = click.prompt("Password", hide_input=True)
    return make_client(server=server, username=username, password=password)


def _connect_with_cached_token(ctx, server: str, cached: dict):
    host = token_store.server_host(server)
    client = make_token_client(server=server)
    token_store.apply_to_client(client, cached)
    try:
        # Eager: refreshes an expired access token now (via the refresh token),
        # so a dead session fails here with a clear message instead of mid-command.
        client.auth_headers()
    except ERClientException as e:
        raise ERClientException(
            f"cached session for {host} expired or invalid — run 'er auth login'"
        ) from e

    def _persist_rotation():
        auth = getattr(client, "auth", None) or {}
        if auth.get("access_token") and auth["access_token"] != cached["access_token"]:
            token_store.save_token(host, auth, client.auth_expires, cached.get("username") or "")

    ctx.call_on_close(_persist_rotation)
    return client


@click.group()
@click.option("--server", envvar="ER_SERVER", help="ER site name (myreserve) or full https:// URL.")
@click.option("--username", envvar="ER_USERNAME", help="EarthRanger username.")
@click.option(
    "--password", envvar="ER_PASSWORD", help="EarthRanger password (prompted if omitted)."
)
@click.option("--profile", envvar="ER_PROFILE", help="Named profile to use (see 'er profile').")
@click.pass_context
def main(ctx, server, username, password, profile):
    """EarthRanger site management CLI."""
    ctx.obj = {"server": server, "username": username, "password": password, "profile": profile}


@main.group("events")
def events_group():
    """Create and edit event categories, choices, and v2 event types; post events."""


@events_group.command("apply")
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
@click.pass_context
@_api_errors
def auth_login(ctx):
    """Log in with username/password and cache the tokens for this server."""
    server, username = _resolve_connection(ctx)
    password = ctx.obj["password"]
    if not username:
        raise click.UsageError("Missing username: pass --username or set ER_USERNAME.")
    if not password:
        password = click.prompt("Password", hide_input=True)
    host = token_store.server_host(server)
    client = make_client(server=server, username=username, password=password)
    if not client.login():
        click.echo(f"error: login failed for {username!r} at {host}")
        sys.exit(1)
    token_store.save_token(host, client.auth, client.auth_expires, username)
    click.echo(f"Authenticated. Token cached for {host}.")


@auth_group.command("logout")
@click.pass_context
def auth_logout(ctx):
    """Delete the cached token for this server."""
    server, _ = _resolve_connection(ctx)
    host = token_store.server_host(server)
    click.echo("Logged out." if token_store.delete_token(host) else "No cached token.")


@auth_group.command("status")
@click.pass_context
def auth_status(ctx):
    """Report whether a cached token exists for this server, and its expiry."""
    server, _ = _resolve_connection(ctx)
    host = token_store.server_host(server)
    data = token_store.load_token(host)
    if not data:
        click.echo(f"{host}: not authenticated")
        return
    state = "expired" if token_store.is_expired(data) else "valid"
    as_user = f" as {data['username']}" if data.get("username") else ""
    click.echo(f"{host}: {state}{as_user} (access token expires {data['expires_at']})")


@events_group.command("pull")
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


def _auth_state(server: str) -> str:
    data = token_store.load_token(token_store.server_host(server))
    if not data:
        return "not authenticated"
    return "expired" if token_store.is_expired(data) else "valid"


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
    config_store.add_profile(name, server=p_server, username=p_username)
    click.echo(f"Added profile {name!r} ({token_store.server_host(p_server)}).")


@profile_group.command("list")
@click.pass_context
def profile_list(ctx):
    """List profiles: selection marker (--profile/ER_PROFILE), server, username, auth state."""
    profiles = config_store.list_profiles()
    if not profiles:
        click.echo("No profiles. Add one with 'er profile add NAME --server ...'.")
        return
    active_name = ctx.obj.get("profile")
    for name in sorted(profiles):
        p = profiles[name]
        marker = "*" if name == active_name else " "
        username = p.get("username") or "-"
        host = token_store.server_host(p.get("server") or "")
        click.echo(f"{marker} {name:<16} {host:<40} {username:<16} {_auth_state(p['server'])}")


@profile_group.command("remove")
@click.argument("name")
@_api_errors
def profile_remove(name):
    """Delete a profile (its cached token, keyed by host, is left alone)."""
    if not config_store.remove_profile(name):
        raise config_store.ConfigError(f"no profile named {name!r}")
    click.echo(f"Removed profile {name!r}.")


@profile_group.command("current")
@click.pass_context
def profile_current(ctx):
    """Print this invocation's selected profile (--profile/ER_PROFILE); exit 1 if none."""
    name = ctx.obj.get("profile")
    if not name:
        sys.exit(1)
    click.echo(name)
