"""Read-only `er <resource> <action>` commands, declared as data.

Each ReadCommand row names an ER GET endpoint and the query params it takes;
`register` compiles the rows into click commands and attaches them to the
main group. Flags are hand-written here (a curated subset of the OpenAPI
spec); switching to spec-derived flags is a P1 item in the parity spec.

Output is always er-cli's {"records": [...], "meta": {...}} JSON (see
output.emit) so agent skills can consume it unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import click
import requests
from erclient.er_errors import ERClientException

from . import clock as _clock
from .output import check_format, emit, output_options, parse_fields
from .read import fetch, follow_pages


@dataclass(frozen=True)
class Flag:
    param: str  # ER query parameter name, e.g. "updated_since"
    help: str
    kind: str = "str"  # "str" | "int" | "float" | "bool" | "list" | "positive_int"
    # "list": a comma-separated value split into repeated query parameters
    # (?state=a&state=b), which is how DRF's getlist() expects multi-values;
    # a single "a,b" string would be matched literally and return nothing.


@dataclass(frozen=True)
class ReadCommand:
    group: str  # top-level group, e.g. "subjects" (created if missing)
    name: str  # action, e.g. "search"
    path: str  # relative to the API root; "{id}" is replaced by the positional
    help: str
    kind: str = "list"  # "list" (paginated, has --limit) | "get" (single object)
    flags: tuple[Flag, ...] = ()
    version: str | None = None  # e.g. "v2.0"; None = erclient default (v1.0)
    arg: str | None = None  # positional name shown in help, e.g. "subject_id"
    # Runs on the built query params just before the request: a place for an
    # endpoint to refuse an unbounded request or fill in a sensible default.
    # Raise click.UsageError to refuse; return the (possibly amended) params.
    prepare: Callable[[dict], dict] | None = None
    # Unwraps an endpoint-specific response envelope before pagination and
    # metadata are computed (see read.fetch). Leave None for DRF-shaped pages.
    unwrap: Callable[[Any], Any] | None = None
    # Runs on the query params after connecting, for lookups that need the
    # server (e.g. turning event-type names into the ids the API filters on).
    # Raise click.UsageError to refuse; return the (possibly amended) params.
    resolve: Callable[[Any, dict], dict] | None = None


@dataclass(frozen=True)
class Deps:
    """What the commands need from cli.py, injected so this module never imports it
    (and so tests that monkeypatch cli._connect keep working)."""

    connect: Callable[[click.Context], object]
    connection_options: Callable
    api_errors: Callable


GROUP_HELP: dict[str, str] = {
    "status": "Server status.",
    "subjects": "Read subjects (animals / collars).",
    "tracks": "Read subject tracks (GeoJSON).",
    "observations": "Read raw GPS observations.",
    "patrols": "Read patrols.",
    "sources": "Read collar / device sources.",
    "subject-groups": "Read subject groups.",
    "subject-sources": "Read subject-to-source assignments.",
    "spatial-feature-groups": "Read spatial feature groups (named collections of spatial features).",
    "spatial-features": "Read spatial features (geofences, roads, water points, boundaries...).",
    "featuresets": "Read featuresets (GeoJSON boundaries).",
    "regions": "Read operational regions.",
}

_PAGE_SIZE = Flag("page_size", "Records per page (default 100).", "positive_int")

OBSERVATION_SELECTORS = ("subject_id", "source_id", "subjectsource_id", "sourceprovider_id")
OBSERVATIONS_DEFAULT_WINDOW = timedelta(hours=24)


def _unwrap_featureset_list(page: Any) -> Any:
    """ER's featureset list is {"features": [...]} from an unpaginated view."""
    if isinstance(page, dict) and "features" in page and "results" not in page:
        return list(page.get("features") or [])
    return page


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _all_event_types(client) -> list[dict]:
    """Every event type on the server, both schema versions.

    das serves v1 and v2 types from separate listings — the v1 endpoint
    filters to version 1 and the v2 endpoint to version 2 — so a type created
    by this CLI (always v2) is missing from the v1 list and vice versa. The
    v2 listing is a plain list today; it is run through the page normalizer
    so a paginated response would also work.
    """
    v1 = client.get_event_types(include_inactive=True)
    v2_page = client.get_event_types(include_inactive=True, version="v2.0")
    v2, _, _, _ = follow_pages(client, v2_page)
    return [t for t in list(v1 or []) + list(v2) if isinstance(t, dict)]


def _resolve_event_types(client, params: dict) -> dict:
    """Let --event-type take values (`geofence_break`) or display names
    (`Geofence Break`) as well as ids: the events endpoint filters on
    event-type *id*, so names are mapped through the event-types listings.
    Ids pass through untouched, so names and ids can be mixed. An exact
    value match is authoritative; a display name is accepted only when it
    matches exactly one type, since das does not require them to be unique."""
    wanted = params.get("event_type")
    if not wanted or all(_UUID_RE.match(w) for w in wanted):
        return params
    types = _all_event_types(client)
    by_value: dict[str, str] = {}
    by_display: dict[str, list[dict]] = {}
    for t in types:
        if not t.get("id"):
            continue
        if t.get("value"):
            by_value[t["value"]] = t["id"]
        if t.get("display"):
            by_display.setdefault(str(t["display"]).casefold(), []).append(t)
    resolved: list[str] = []
    for w in wanted:
        if _UUID_RE.match(w):
            resolved.append(w)
            continue
        if w in by_value:
            resolved.append(by_value[w])
            continue
        matches = by_display.get(w.casefold(), [])
        if len(matches) == 1:
            resolved.append(matches[0]["id"])
            continue
        if len(matches) > 1:
            options = ", ".join(sorted(f"{m.get('value')} ({m['id']})" for m in matches))
            raise click.UsageError(
                f"display name {w!r} matches {len(matches)} event types: {options}. "
                "Pass the value or id instead."
            )
        available = ", ".join(sorted(by_value))
        raise click.UsageError(
            f"unknown event type {w!r}. Pass a value, display name or id; "
            f"values on this server: {available}"
        )
    params["event_type"] = resolved
    return params


def _parse_iso(value: str, flag: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)  # Python 3.11+ accepts a trailing Z
    except ValueError:
        raise click.UsageError(f"{flag} must be an ISO-8601 timestamp, got {value!r}.") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _prepare_observations(params: dict) -> dict:
    """Keep `observations search` bounded.

    With no selector ER falls through to "every observation on the site up to
    now", which the CLI would then page through in full; das itself rejects
    more than one selector. With a selector but no `since`, ER defaults the
    start to a day before `until` — we do the same client-side so the window
    is visible in the request and in the stderr note.
    """
    chosen = [p for p in OBSERVATION_SELECTORS if params.get(p)]
    flags = ", ".join("--" + p.replace("_", "-") for p in OBSERVATION_SELECTORS)
    if not chosen:
        raise click.UsageError(
            f"observations search needs exactly one of {flags}; without one ER returns "
            "every observation on the site."
        )
    if len(chosen) > 1:
        raise click.UsageError(f"pass only one of {flags} (got {', '.join(chosen)}).")
    if not params.get("since"):
        until = params.get("until")
        end = _parse_iso(until, "--until") if until else datetime.now(UTC)
        # `end` keeps whatever offset --until carried; convert before stamping a Z
        start = (end - OBSERVATIONS_DEFAULT_WINDOW).astimezone(UTC)
        params["since"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        anchor = f"--until {until}" if until else "now"
        click.echo(
            f"note: no --since given; defaulting to the 24 hours before {anchor} "
            f"({params['since']}).",
            err=True,
        )
    return params


_INCLUDE_INACTIVE = Flag("include_inactive", "Include inactive records.", "bool")

COMMANDS: tuple[ReadCommand, ...] = (
    ReadCommand("status", "show", "status", "Server status and current server time.", "get"),
    ReadCommand("auth", "whoami", "user/me", "Show the authenticated user (/user/me/).", "get"),
    ReadCommand(
        "subjects",
        "search",
        "subjects",
        "Search subjects.",
        flags=(
            Flag("name", "Filter by subject name."),
            Flag("subject_group", "Subject group id."),
            Flag("subject_subtypes", "Comma-separated subject subtypes."),
            _INCLUDE_INACTIVE,
            Flag("updated_since", "ISO-8601; subjects updated after this time."),
            Flag("position_updated_since", "ISO-8601; subjects with a position after this time."),
            Flag("render_last_location", "Include each subject's last location.", "bool"),
            Flag("tracks", "Include recent tracks.", "bool"),
            Flag("tracks_since", "ISO-8601 start for --tracks."),
            Flag("tracks_until", "ISO-8601 end for --tracks."),
            Flag("bbox", "Bounding box: west,south,east,north."),
            _PAGE_SIZE,
        ),
    ),
    ReadCommand(
        "subjects", "get", "subject/{id}", "Retrieve one subject.", "get", arg="subject_id"
    ),
    ReadCommand(
        "tracks",
        "get",
        "subject/{id}/tracks",
        "Retrieve a subject's track (GeoJSON) for a time window.",
        "get",
        flags=(
            Flag("since", "ISO-8601 start."),
            Flag("until", "ISO-8601 end."),
            Flag("show_excluded", "Include points ER excluded as outliers.", "bool"),
            Flag("max_speed_kmh", "Drop segments faster than this.", "float"),
            Flag("max_gap_minutes", "Break the track at gaps longer than this.", "int"),
        ),
        version="v2.0",
        arg="subject_id",
    ),
    ReadCommand(
        "observations",
        "search",
        "observations",
        "Search raw GPS observations for one subject, source, assignment, or provider.\n\n"
        "Exactly one selector is required; --since defaults to the last 24 hours.",
        flags=(
            Flag("subject_id", "Subject id (selector)."),
            Flag("source_id", "Source id (selector)."),
            Flag("subjectsource_id", "Subject-source assignment id (selector)."),
            Flag("sourceprovider_id", "Source provider id (selector)."),
            Flag("since", "ISO-8601 start (default: 24 hours ago)."),
            Flag("until", "ISO-8601 end (default: now)."),
            Flag("filter", "ER observation filter (e.g. 0 = exclusion flags off)."),
            Flag("include_details", "Include observation details.", "bool"),
            Flag("bbox", "Bounding box: west,south,east,north."),
            Flag("sort_by", "Sort field (prefix '-' for descending)."),
            _PAGE_SIZE,
        ),
        prepare=_prepare_observations,
    ),
    ReadCommand(
        "events",
        "search",
        "activity/events",
        "Search activity events.",
        flags=(
            Flag(
                "event_type",
                "Event type value(s), display name(s) or id(s), comma-separated; "
                "names are resolved to ids for you.",
                "list",
            ),
            Flag("event_category", "Event category value."),
            Flag("state", "new | active | resolved (comma-separated allowed).", "list"),
            Flag("updated_since", "ISO-8601; events updated after this time."),
            Flag("filter", "ER events JSON filter."),
            Flag("bbox", "Bounding box: west,south,east,north."),
            Flag("sort_by", "Sort field (prefix '-' for descending)."),
            Flag("include_details", "Include event_details.", "bool"),
            Flag("include_notes", "Include notes.", "bool"),
            Flag("include_updates", "Include the update history.", "bool"),
            Flag("include_files", "Include attached files.", "bool"),
            Flag("include_related_events", "Include related events.", "bool"),
            Flag("is_collection", "Only incident collections.", "bool"),
            Flag("exclude_contained", "Exclude events contained in a collection.", "bool"),
            _PAGE_SIZE,
        ),
        resolve=_resolve_event_types,
    ),
    ReadCommand(
        "events", "get", "activity/event/{id}", "Retrieve one event.", "get", arg="event_id"
    ),
    ReadCommand(
        "patrols",
        "search",
        "activity/patrols",
        "Search patrols.",
        flags=(
            Flag("filter", "ER patrols JSON filter."),
            Flag("state", "open | done | cancelled."),
            Flag("exclude_empty_patrols", "Skip patrols with no segments.", "bool"),
            _PAGE_SIZE,
        ),
    ),
    ReadCommand(
        "patrols", "get", "activity/patrols/{id}", "Retrieve one patrol.", "get", arg="patrol_id"
    ),
    ReadCommand(
        "sources", "search", "sources", "Search collar / device sources.", flags=(_PAGE_SIZE,)
    ),
    ReadCommand("sources", "get", "source/{id}", "Retrieve one source.", "get", arg="source_id"),
    ReadCommand(
        "subject-groups",
        "list",
        "subjectgroups",
        "List subject groups.",
        flags=(
            _INCLUDE_INACTIVE,
            Flag("include_hidden", "Include groups that are not visible.", "bool"),
            Flag("flat", "Flatten nested groups into one list.", "bool"),
            Flag("group_name", "Filter by group name."),
            _PAGE_SIZE,
        ),
    ),
    ReadCommand(
        "subject-groups",
        "get",
        "subjectgroup/{id}",
        "Retrieve one subject group.",
        "get",
        arg="group_id",
    ),
    ReadCommand(
        "subject-sources",
        "search",
        "subjectsources",
        "Search subject-to-source assignments (which collar tracked which subject when).",
        flags=(
            Flag("subjects", "Comma-separated subject ids."),
            Flag("sources", "Comma-separated source ids."),
            _PAGE_SIZE,
        ),
    ),
    # ER serves the v1 mapping read endpoints below with Deprecation/Sunset
    # headers; they are still the only way to read these objects over the API.
    ReadCommand(
        "spatial-feature-groups",
        "list",
        "spatialfeaturegroup",
        "List spatial feature groups.",
        flags=(
            Flag("sort_by", "name | created_at | updated_at (prefix '-' to reverse)."),
            _PAGE_SIZE,
        ),
    ),
    ReadCommand(
        "spatial-feature-groups",
        "get",
        "spatialfeaturegroup/{id}",
        "Retrieve one spatial feature group.",
        "get",
        arg="group_id",
    ),
    ReadCommand(
        "spatial-features",
        "list",
        "spatialfeature",
        "List spatial features.",
        flags=(
            Flag("feature_class", "Feature type id(s), comma-separated."),
            Flag("display_category", "Display category id(s), comma-separated."),
            Flag("sort_by", "name (prefix '-' to reverse)."),
            _PAGE_SIZE,
        ),
    ),
    ReadCommand(
        "spatial-features",
        "get",
        "spatialfeature/{id}",
        "Retrieve one spatial feature (GeoJSON).",
        "get",
        arg="feature_id",
    ),
    ReadCommand(
        "featuresets",
        "list",
        "featureset",
        "List featuresets (id, name, feature types). Not paginated on the server.",
        flags=(
            Flag("include_hidden", "Include feature types that are not visible.", "bool"),
            Flag("summarize_features", "Include a feature summary per type.", "bool"),
        ),
        unwrap=_unwrap_featureset_list,
    ),
    ReadCommand(
        "featuresets",
        "get",
        "featureset/{id}",
        "Retrieve a featureset (GeoJSON boundaries).",
        "get",
        arg="featureset_id",
    ),
    ReadCommand("regions", "list", "regions", "List operational regions.", flags=(_PAGE_SIZE,)),
)

_TYPES = {"str": str, "int": int, "float": float, "positive_int": click.IntRange(min=1)}


def get_clock(ctx, client, *, required: bool) -> dict | None:
    """The site clock, fetched once per invocation. Passive callers (meta
    enrichment) get None when /status fails; a window flag that needs the
    clock passes required=True and lets the error surface."""
    cached = ctx.obj.get("clock")
    if cached is not None:
        return cached
    try:
        info = _clock.fetch_clock(client)
    except (ERClientException, requests.exceptions.RequestException):
        if required:
            raise
        return None
    ctx.obj["clock"] = info
    return info


def _option_names(param: str) -> list[str]:
    """`--updated-since` plus er-cli's `--updated_since`; the last entry is the
    Python destination name click passes to the callback."""
    names = ["--" + param.replace("_", "-")]
    if "_" in param:
        names.append("--" + param)
    names.append(param)
    return names


def _endpoint(spec: ReadCommand) -> str:
    path = spec.path.replace("{id}", "{" + (spec.arg or "id") + "}")
    return f"[GET /api/{spec.version or 'v1.0'}/{path}]"


def _make_command(spec: ReadCommand, deps: Deps) -> click.Command:
    def callback(ctx, output, fields=None, fmt="json", limit=None, **kwargs):
        fields = parse_fields(fields)
        check_format(fields, fmt)
        # Build and validate the request before connecting: a usage error (a
        # missing selector, an unparseable --until) must not first demand a
        # server or prompt for a password.
        path = spec.path
        if spec.arg:
            path = path.replace("{id}", quote(kwargs.pop(spec.arg), safe=""))
        params: dict = {}
        for flag in spec.flags:
            value = kwargs.get(flag.param)
            if flag.kind == "bool":
                if value:
                    params[flag.param] = "true"
            elif flag.kind == "list":
                if value is not None:
                    params[flag.param] = [v.strip() for v in value.split(",") if v.strip()]
            elif value is not None:
                params[flag.param] = value
        if spec.prepare is not None:
            params = spec.prepare(params)
        client = deps.connect(ctx)
        if spec.resolve is not None:
            params = spec.resolve(client, params)
        records, meta = fetch(
            client,
            path,
            params,
            paginate=spec.kind == "list",
            limit=limit,
            version=spec.version,
            unwrap=spec.unwrap,
        )
        info = get_clock(ctx, client, required=False)
        if info:
            meta.update(_clock.clock_meta(info))
        emit(records, meta, output, fields=fields, fmt=fmt)

    callback.__name__ = f"{spec.group}_{spec.name}".replace("-", "_")
    fn = deps.api_errors(callback)
    fn = click.pass_context(fn)
    for flag in reversed(spec.flags):
        if flag.kind == "bool":
            fn = click.option(*_option_names(flag.param), is_flag=True, help=flag.help)(fn)
        else:
            fn = click.option(
                *_option_names(flag.param), type=_TYPES.get(flag.kind, str), help=flag.help
            )(fn)
    if spec.kind == "list":
        fn = click.option(
            "--limit", type=click.IntRange(min=1), metavar="N", help="Cap total records returned."
        )(fn)
    fn = click.option(
        "-o", "--output", type=click.Path(dir_okay=False), help="Write JSON here instead of stdout."
    )(fn)
    fn = output_options(fn)
    if spec.arg:
        fn = click.argument(spec.arg)(fn)
    fn = deps.connection_options(fn)
    return click.command(spec.name, help=f"{spec.help}\n\n{_endpoint(spec)}")(fn)


# Every paginated command answers to both names: er-cli (and the tusker skills
# written against it) say `search`; `list` is what people reach for first.
# The row's own name is the one shown in the endpoint docs; the other is an
# alias to the same Command object.
_LIST_ALIASES = {"search": "list", "list": "search"}


def register(main: click.Group, deps: Deps) -> None:
    """Attach every COMMANDS row to `main`, creating resource groups as needed.
    Rows whose group already exists (events, auth) are added to that group."""
    for spec in COMMANDS:
        group = main.commands.get(spec.group)
        if group is None:
            group = click.Group(spec.group, help=GROUP_HELP[spec.group])
            main.add_command(group)
        cmd = _make_command(spec, deps)
        group.add_command(cmd)
        alias = _LIST_ALIASES.get(spec.name) if spec.kind == "list" else None
        if alias and alias not in group.commands:
            group.add_command(cmd, name=alias)
    main.add_command(_make_now(deps))


def _make_now(deps: Deps) -> click.Command:
    """`er now`: the clock every window flag is computed from, so an agent
    never does timezone arithmetic by hand."""

    def now(ctx, output, fields=None, fmt="json"):
        fields = parse_fields(fields)
        check_format(fields, fmt)
        client = deps.connect(ctx)
        info = get_clock(ctx, client, required=True)
        record = {
            "utc": info["utc"],
            "site_now": info["local"],
            "site_tz": info.get("timezone_name") or info.get("timezone"),
            "today": info["today"],
        }
        emit([record], {"total": 1, "pages": 1}, output, fields=fields, fmt=fmt)

    fn = deps.api_errors(now)
    fn = click.pass_context(fn)
    fn = click.option(
        "-o", "--output", type=click.Path(dir_okay=False), help="Write JSON here instead of stdout."
    )(fn)
    fn = output_options(fn)
    fn = deps.connection_options(fn)
    return click.command(
        "now",
        help="The server's current time (UTC), the site's local time and timezone, and "
        "today's --since/--until bounds in site time.\n\n[GET /api/v1.0/status]",
    )(fn)
