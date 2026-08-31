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
from . import token_store
from .apply import ApplyError, apply_spec, extract_choice_fields
from .client import make_client, make_token_client
from .dsl import SpecError, load_spec
from .events import FieldArgError, build_event, load_events_file, parse_field_args, post_events


def _api_errors(f):
    """Turn API failures (auth, writes) into a clean message and exit 1."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except (ApplyError, ERClientException, requests.exceptions.RequestException) as e:
            click.echo(f"error: {e}")
            sys.exit(1)

    return wrapper


def _require_server(ctx) -> str:
    server = ctx.obj["server"]
    if not server:
        raise click.UsageError("Missing server: pass --server or set ER_SERVER.")
    return server


def _connect(ctx):
    """Build an authenticated client.

    Precedence: an explicit password (flag or ER_PASSWORD) wins; else a token
    cached by `er-events auth login`; else an interactive password prompt.
    """
    server = _require_server(ctx)
    username = ctx.obj["username"]
    password = ctx.obj["password"]
    if not password:
        cached = token_store.load_token(token_store.server_host(server))
        if cached:
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
            f"cached session for {host} expired or invalid — run 'er-events auth login'"
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
@click.pass_context
def main(ctx, server, username, password):
    """Create and edit EarthRanger event categories, choices, and v2 event types."""
    ctx.obj = {"server": server, "username": username, "password": password}


@main.command("apply")
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


@main.command("post-event")
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


@main.group("list")
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


@main.group("show")
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
    fields = extract_choice_fields(et.get("schema") or {})
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
    server = _require_server(ctx)
    username = ctx.obj["username"]
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
    host = token_store.server_host(_require_server(ctx))
    click.echo("Logged out." if token_store.delete_token(host) else "No cached token.")


@auth_group.command("status")
@click.pass_context
def auth_status(ctx):
    """Report whether a cached token exists for this server, and its expiry."""
    host = token_store.server_host(_require_server(ctx))
    data = token_store.load_token(host)
    if not data:
        click.echo(f"{host}: not authenticated")
        return
    state = "expired" if token_store.is_expired(data) else "valid"
    as_user = f" as {data['username']}" if data.get("username") else ""
    click.echo(f"{host}: {state}{as_user} (access token expires {data['expires_at']})")
