# src/earthranger_cli/read_commands.py
"""Read-only `er <resource> <action>` commands, declared as data.

Each ReadCommand row names an ER GET endpoint and the query params it takes;
`register` compiles the rows into click commands and attaches them to the
main group. Flags are hand-written here (a curated subset of the OpenAPI
spec); switching to spec-derived flags is a P1 item in the parity spec.

Output is always er-cli's {"records": [...], "meta": {...}} JSON (see
output.emit) so agent skills can consume it unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import click

from .output import emit
from .read import fetch


@dataclass(frozen=True)
class Flag:
    param: str  # ER query parameter name, e.g. "updated_since"
    help: str
    kind: str = "str"  # "str" | "int" | "float" | "bool"


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
    "fences": "Read spatial feature groups (geofences).",
    "featuresets": "Read featuresets (GeoJSON boundaries).",
    "regions": "Read operational regions.",
}

_PAGE_SIZE = Flag("page_size", "Records per page (default 100).", "int")
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
    ReadCommand("subjects", "get", "subject/{id}", "Retrieve one subject.", "get", arg="subject_id"),
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
            Flag("max_gap_minutes", "Break the track at gaps longer than this.", "float"),
        ),
        version="v2.0",
        arg="subject_id",
    ),
    ReadCommand(
        "observations",
        "search",
        "observations",
        "Search raw GPS observations.",
        flags=(
            Flag("subject_id", "Subject id."),
            Flag("source_id", "Source id."),
            Flag("subjectsource_id", "Subject-source assignment id."),
            Flag("since", "ISO-8601 start."),
            Flag("until", "ISO-8601 end."),
            Flag("filter", "ER observation filter (e.g. 0 = exclusion flags off)."),
            Flag("include_details", "Include observation details.", "bool"),
            Flag("bbox", "Bounding box: west,south,east,north."),
            Flag("sort_by", "Sort field (prefix '-' for descending)."),
            _PAGE_SIZE,
        ),
    ),
    ReadCommand(
        "events",
        "search",
        "activity/events",
        "Search activity events.",
        flags=(
            Flag("event_type", "Event type id(s), comma-separated (ids, not values — see P1)."),
            Flag("event_category", "Event category value."),
            Flag("state", "new | active | resolved (comma-separated allowed)."),
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
    ),
    ReadCommand("events", "get", "activity/event/{id}", "Retrieve one event.", "get", arg="event_id"),
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
    ReadCommand("patrols", "get", "activity/patrols/{id}", "Retrieve one patrol.", "get", arg="patrol_id"),
    ReadCommand("sources", "search", "sources", "Search collar / device sources.", flags=(_PAGE_SIZE,)),
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
        ),
    ),
    ReadCommand(
        "subject-groups", "get", "subjectgroup/{id}", "Retrieve one subject group.", "get", arg="group_id"
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
    ReadCommand(
        "fences",
        "list",
        "spatialfeaturegroup",
        "List spatial feature groups (geofences).",
        flags=(Flag("sort_by", "Sort field."),),
    ),
    ReadCommand(
        "featuresets", "get", "featureset/{id}", "Retrieve a featureset (GeoJSON boundaries).", "get",
        arg="featureset_id",
    ),
    ReadCommand("regions", "list", "regions", "List operational regions."),
)

_TYPES = {"str": str, "int": int, "float": float}


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
    def callback(ctx, output, limit=None, **kwargs):
        client = deps.connect(ctx)
        path = spec.path
        if spec.arg:
            path = path.replace("{id}", kwargs.pop(spec.arg))
        params: dict = {}
        for flag in spec.flags:
            value = kwargs.get(flag.param)
            if flag.kind == "bool":
                if value:
                    params[flag.param] = "true"
            elif value is not None:
                params[flag.param] = value
        records, meta = fetch(
            client, path, params, paginate=spec.kind == "list", limit=limit, version=spec.version
        )
        emit(records, meta, output)

    callback.__name__ = f"{spec.group}_{spec.name}".replace("-", "_")
    fn = deps.api_errors(callback)
    fn = click.pass_context(fn)
    for flag in reversed(spec.flags):
        if flag.kind == "bool":
            fn = click.option(*_option_names(flag.param), is_flag=True, help=flag.help)(fn)
        else:
            fn = click.option(*_option_names(flag.param), type=_TYPES[flag.kind], help=flag.help)(fn)
    if spec.kind == "list":
        fn = click.option("--limit", type=int, metavar="N", help="Cap total records returned.")(fn)
    fn = click.option(
        "-o", "--output", type=click.Path(dir_okay=False), help="Write JSON here instead of stdout."
    )(fn)
    if spec.arg:
        fn = click.argument(spec.arg)(fn)
    fn = deps.connection_options(fn)
    return click.command(spec.name, help=f"{spec.help}\n\n{_endpoint(spec)}")(fn)


def register(main: click.Group, deps: Deps) -> None:
    """Attach every COMMANDS row to `main`, creating resource groups as needed.
    Rows whose group already exists (events, auth) are added to that group."""
    for spec in COMMANDS:
        group = main.commands.get(spec.group)
        if group is None:
            group = click.Group(spec.group, help=GROUP_HELP[spec.group])
            main.add_command(group)
        group.add_command(_make_command(spec, deps))
