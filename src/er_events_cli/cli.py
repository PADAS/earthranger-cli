"""Click CLI wiring. Logic lives in the other modules; this file only parses
flags, connects, and formats output."""

from __future__ import annotations

import functools
import sys

import click
from erclient.er_errors import ERClientException

from .apply import ApplyError, apply_spec
from .client import make_client
from .dsl import SpecError, load_spec


def _api_errors(f):
    """Turn API failures (auth, writes) into a clean message and exit 1."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except (ApplyError, ERClientException) as e:
            click.echo(f"error: {e}")
            sys.exit(1)

    return wrapper


def _connect(ctx):
    server = ctx.obj["server"]
    username = ctx.obj["username"]
    password = ctx.obj["password"]
    if not server:
        raise click.UsageError("Missing server: pass --server or set ER_SERVER.")
    if not username:
        raise click.UsageError("Missing username: pass --username or set ER_USERNAME.")
    if not password:
        password = click.prompt("Password", hide_input=True)
    return make_client(server=server, username=username, password=password)


@click.group()
@click.option("--server", envvar="ER_SERVER",
              help="ER site name (myreserve) or full https:// URL.")
@click.option("--username", envvar="ER_USERNAME", help="EarthRanger username.")
@click.option("--password", envvar="ER_PASSWORD",
              help="EarthRanger password (prompted if omitted).")
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
