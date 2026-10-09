"""Read-only `er <resource> <action>` commands, declared as data.

Each ReadCommand row names an ER GET endpoint and the query params it takes;
`register` compiles the rows into click commands and attaches them to the
main group. Flags are hand-written here (a curated subset of the OpenAPI
spec); switching to spec-derived flags is a P1 item in the parity spec.

Output is always er-cli's {"records": [...], "meta": {...}} JSON (see
output.emit) so agent skills can consume it unchanged.
"""

from __future__ import annotations

import csv
import difflib
import fnmatch
import io
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import click
from erclient.er_errors import ERClientPermissionDenied

from . import aggregate
from . import clock as _clock
from . import windows as _windows
from .output import check_format, emit, output_options, parse_fields
from .read import fetch, fetch_count, fetch_text, follow_pages


@dataclass(frozen=True)
class Flag:
    param: str  # ER query parameter name, e.g. "updated_since"
    help: str
    kind: str = "str"  # "str" | "int" | "float" | "bool" | "list" | "positive_int" | "multi"
    # "multi": a repeatable option handled by the CLI itself, never sent as a query param
    # "list": a comma-separated value split into repeated query parameters
    # (?state=a&state=b), which is how DRF's getlist() expects multi-values;
    # a single "a,b" string would be matched literally and return nothing.


@dataclass(frozen=True)
class ReadCommand:
    group: str  # top-level group, e.g. "subjects" (created if missing)
    name: str  # action, e.g. "search"
    path: str  # relative to the API root; "{id}" is replaced by the positional
    help: str
    # "list" (paginated, has --limit) | "get" (single object) | "raw" (the body
    # is written as the server sent it — the CSV exports)
    kind: str = "list"
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
    # How --since/--until (and --today/--yesterday/--last) reach this endpoint:
    # one of windows.KINDS, or None for commands without a time window.
    window: str | None = None
    # Fill --since this far before --until (or now) when no window flag is given.
    default_window: timedelta | None = None
    # Dotted path to a record's timestamp, for --group-by day|week|month.
    time_field: str | None = None
    # For "raw" exports: how to answer with the records endpoint instead when
    # the account may not export (403). {"path", "keep": {export_param:
    # records_param}, "drop": {params that mean nothing there}, "add": {...}}.
    # A sent param in neither keep nor drop, or a missing "require"d one, means
    # the fallback cannot carry the question, and the 403 stands rather than
    # widening it.
    fallback: dict | None = None
    # Query params a `--where` filter needs the server to include (the detail
    # it reads), declared on the row rather than assumed by the shared callback.
    where_params: dict = field(default_factory=dict)


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
    seen: set = set()
    types = []
    for t in list(v1 or []) + list(v2):
        # the same type must not count twice when a listing repeats it
        if isinstance(t, dict) and t.get("id") not in seen:
            seen.add(t.get("id"))
            types.append(t)
    return types


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
        if any(ch in w for ch in "*?["):
            # a pattern: every type whose value or display matches, named on stderr
            pat = w.casefold()
            hits = [
                t
                for t in types
                if t.get("id")
                and (
                    fnmatch.fnmatchcase(str(t.get("value") or "").casefold(), pat)
                    or fnmatch.fnmatchcase(str(t.get("display") or "").casefold(), pat)
                )
            ]
            if not hits:
                raise click.UsageError(
                    f"--event-type {w!r} matches no event type on this server; "
                    f"values: {', '.join(sorted(by_value))}"
                )
            resolved.extend(t["id"] for t in hits)
            matched = ", ".join(sorted(str(t.get("value")) for t in hits))
            click.echo(
                f"note: --event-type {w!r} matched {len(hits)} type(s): {matched}.", err=True
            )
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
        displays = [str(t["display"]) for t in types if t.get("display")]
        close = difflib.get_close_matches(w, list(by_value) + displays, n=3, cutoff=0.6)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise click.UsageError(
            f"unknown event type {w!r}.{hint} Pass a value, display name or id; "
            f"values on this server: {available}"
        )
    params["event_type"] = resolved
    return params


def _resolve_subject_group(client, params: dict) -> dict:
    """Let --subject-group take a group name as well as an id."""
    wanted = params.get("subject_group")
    if not wanted or _UUID_RE.match(wanted):
        return params
    groups, _ = fetch(
        client, "subjectgroups", {"flat": "true", "include_inactive": "true"}, paginate=True
    )
    hits = [
        g
        for g in groups
        if isinstance(g, dict)
        and g.get("id")
        and str(g.get("name") or "").casefold() == wanted.casefold()
    ]
    if len(hits) == 1:
        params["subject_group"] = hits[0]["id"]
        return params
    if hits:
        options = ", ".join(sorted(f"{g['name']} ({g['id']})" for g in hits))
        raise click.UsageError(
            f"subject group {wanted!r} matches {len(hits)} groups: {options}. Pass the id."
        )
    names = sorted(str(g.get("name")) for g in groups if isinstance(g, dict) and g.get("name"))
    close = difflib.get_close_matches(wanted, names, n=3, cutoff=0.6)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise click.UsageError(
        f"unknown subject group {wanted!r}.{hint} Groups on this server: {', '.join(names)}"
    )


def _resolve_export_event_types(client, params: dict) -> dict:
    """The export view reads event types only from inside `filter`."""
    params = _resolve_event_types(client, params)
    ids = params.pop("event_type", None)
    if ids:
        _windows.merge_filter(params, event_type=list(ids))
    return params


def _prepare_observations(params: dict) -> dict:
    """Keep `observations search` bounded.

    With no selector ER falls through to "every observation on the site up to
    now", which the CLI would then page through in full; das itself rejects
    more than one selector. (The 24-hour default for `since` is the row's
    `default_window`, applied by the window machinery.)
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
            Flag("subject_group", "Subject group name or id."),
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
        resolve=_resolve_subject_group,
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
            Flag("show_excluded", "Include points ER excluded as outliers.", "bool"),
            Flag("max_speed_kmh", "Drop segments faster than this.", "float"),
            Flag("max_gap_minutes", "Break the track at gaps longer than this.", "int"),
        ),
        version="v2.0",
        arg="subject_id",
        window="since_until",
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
            Flag("filter", "ER observation filter (e.g. 0 = exclusion flags off)."),
            Flag("include_details", "Include observation details.", "bool"),
            Flag("bbox", "Bounding box: west,south,east,north."),
            Flag("sort_by", "Sort field (prefix '-' for descending)."),
            _PAGE_SIZE,
        ),
        prepare=_prepare_observations,
        window="since_until",
        default_window=OBSERVATIONS_DEFAULT_WINDOW,
        time_field="recorded_at",
    ),
    ReadCommand(
        "observations",
        "export",
        "trackingdata/export",
        "Export raw observations as the server's CSV (ER's own column names).",
        "raw",
        flags=(
            Flag("subject_id", "Subject id."),
            Flag("source_provider", "Source provider key."),
            Flag("subject_chronofile", "Subject chronofile number."),
            Flag("filter", "ER observation filter (e.g. 0 = exclusion flags off)."),
            _INCLUDE_INACTIVE,
            Flag(
                "current_status", "One row per subject: its current status, not its track.", "bool"
            ),
            Flag("record_serial_base", "Serial base for record numbering.", "int"),
            Flag("max_records", "Stop after this many records.", "int"),
        ),
        window="after_before",
        fallback={
            "path": "observations",
            "keep": {"after_date": "since", "before_date": "until", "subject_id": "subject_id"},
            "drop": set(),
            "add": {"include_details": "true"},
            # without a selector the records endpoint is every observation on
            # the site (what `observations search` refuses); the 403 stands
            "require": {"subject_id"},
        },
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
            Flag(
                "where",
                "Keep events whose event_details say KEY is VALUE (repeatable), e.g. "
                "--where species=buffalo. Applied by the CLI after fetching; with "
                "--count-only or --group-by the CLI counts the matches.",
                "multi",
            ),
            _PAGE_SIZE,
        ),
        resolve=_resolve_event_types,
        window="filter",
        time_field="time",
        where_params={"include_details": "true"},
    ),
    ReadCommand(
        "events", "get", "activity/event/{id}", "Retrieve one event.", "get", arg="event_id"
    ),
    ReadCommand(
        "events",
        "export",
        "activity/events/export",
        "Export events as the server's own CSV: columns and values are the site's display "
        "names (what the form shows), which the JSON records do not carry.",
        "raw",
        flags=(
            Flag("filter", "ER events JSON filter; --since/--until merge into it."),
            Flag(
                "event_type",
                "Event type value(s), display name(s) or id(s), comma-separated; "
                "resolved to ids and folded into the filter.",
                "list",
            ),
            Flag("state", "new | active | resolved."),
            Flag("bbox", "Bounding box: west,south,east,north."),
            Flag("value_cols", "Also write each field's internal value column.", "bool"),
            Flag(
                "display_cols", "Write fields under their display names (server default).", "bool"
            ),
        ),
        window="filter",
        resolve=_resolve_export_event_types,
        fallback={
            "path": "activity/events",
            "keep": {"filter": "filter", "state": "state", "bbox": "bbox"},
            "drop": {"value_cols", "display_cols"},
            "add": {"include_details": "true"},
            # the CSV export is legitimately whole-site; a detailed JSON walk of
            # every event is not, so the fallback needs a window or a filter
            "require_any": {"filter"},
        },
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
        window="filter",
        time_field="patrol_segments.0.time_range.start_time",
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


def get_clock(ctx, client) -> dict:
    """The site clock, fetched once per invocation and only when something
    needs it (--today/--yesterday/--last, a period --group-by, `er now`):
    a plain read never pays for the extra GET /status."""
    cached = ctx.obj.get("clock")
    if cached is None:
        cached = ctx.obj["clock"] = _clock.fetch_clock(client)
    return cached


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


def _grouped(
    ctx,
    client,
    spec: ReadCommand,
    records: list,
    meta: dict,
    group_by: str,
    window_meta,
    limit: int | None,
    fetched: int,
):
    """Rows of counts in place of records: per field value, or per site-local
    period over the command's window. `fetched` is how many records the walk
    returned before any --where filter; the breakdown is exact only when that
    walk exhausted the query (no truncation, no --limit stop)."""
    out_meta = {
        k: v
        for k, v in meta.items()
        if k in ("pages", "count_reported", "truncated", "note", "where")
    }
    out_meta["group_by"] = group_by
    out_meta["fetched"] = fetched
    out_meta["exact"] = not meta.get("truncated") and (limit is None or fetched < limit)
    if not out_meta["exact"] and not meta.get("truncated"):
        limit_note = (
            f"grouped only the {fetched} record(s) --limit allowed; counts are a floor, not totals."
        )
        out_meta["note"] = (
            f"{out_meta['note']} {limit_note}" if out_meta.get("note") else limit_note
        )
    if group_by in _clock.PERIODS:
        # the window and time_field were checked before connecting (callback)
        info = get_clock(ctx, client)
        tz = _clock.site_tz(info)
        if tz is None:
            raise click.ClickException(
                "the site reported no usable timezone, so --group-by cannot say where a day begins."
            )
        until = window_meta.get("until") or info.get("local") or info["utc"]
        # das reads a naive date inside an events/patrols filter in the site zone,
        # but a naive observations since/until in UTC; bucket the way it windowed
        naive_tz = tz if spec.window == "filter" else UTC
        rows = aggregate.group_by_period(
            records,
            period=group_by,
            since=window_meta["since"],
            until=until,
            tz=tz,
            time_field=spec.time_field,
            naive_tz=naive_tz,
        )
    else:
        rows, matched = aggregate.group_counts(records, group_by)
        if records and not matched:
            keys = ", ".join(aggregate.scalar_keys(records))
            raise click.UsageError(f"no record carries {group_by!r}; fields seen: {keys}")
    out_meta["total"] = len(rows)
    out_meta["records_counted"] = sum(r["count"] for r in rows)
    return rows, out_meta


def _csv_row_count(body: str) -> int | None:
    """Data rows in a CSV body — records, not newlines, since the server quotes
    multi-line notes. None when the parser refuses a field (its default limit
    is 131,072 characters, and das puts all of an event's notes in one field):
    the export itself is fine, only the count is unavailable."""
    if not body.strip():
        return 0
    try:
        return max(sum(1 for _ in csv.reader(io.StringIO(body))) - 1, 0)
    except csv.Error:
        return None


def _emit_raw(
    client, spec: ReadCommand, path: str, params: dict, output: str | None, extra_meta: dict
) -> None:
    """Write the server's body as sent; on 403 answer with the records instead."""
    try:
        body, content_type = fetch_text(client, path, params)
    except ERClientPermissionDenied as e:
        plan = spec.fallback
        mapped = dict(plan["add"]) if plan else None
        if plan:
            for name, value in params.items():
                if name in plan["drop"]:
                    continue
                if name not in plan["keep"]:
                    mapped = None
                    break
                mapped[plan["keep"][name]] = value
            if mapped is not None and not plan.get("require", set()) <= set(mapped):
                mapped = None
            require_any = plan.get("require_any")
            if mapped is not None and require_any and not (require_any & set(mapped)):
                mapped = None
        if mapped is None:
            raise
        note = (
            f"this account may not export ({e}); these are the matching records as JSON "
            f"from GET /api/v1.0/{plan['path']} instead of the CSV."
        )
        click.echo(f"note: {note}", err=True)
        records, meta = fetch(client, plan["path"], mapped, paginate=True)
        meta["note"] = f"{meta['note']} {note}" if meta.get("note") else note
        meta.update(extra_meta)
        emit(records, meta, output)
        return
    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        rows = _csv_row_count(body)
        kind = content_type or "text/csv"
        if rows is None:
            # the file is complete; only the count is missing, so say exactly that
            click.echo(
                f"Done. File written to {output} ({kind}); row count unavailable "
                "(a field exceeded the CSV parser's limit).",
                err=True,
            )
        else:
            click.echo(f"Done. {rows} data row(s) written to {output} ({kind}).", err=True)
    else:
        click.echo(body, nl=False)


def _make_command(spec: ReadCommand, deps: Deps) -> click.Command:
    def callback(ctx, output, fields=None, fmt="json", limit=None, **kwargs):
        fields = parse_fields(fields)
        check_format(fields, fmt)
        count_only = kwargs.pop("count_only", False)
        group_by = kwargs.pop("group_by", None)
        if count_only and group_by:
            raise click.UsageError("pass either --count-only or --group-by, not both.")
        # Build and validate the request before connecting: a usage error (a
        # missing selector, conflicting window flags) must not first demand a
        # server or prompt for a password.
        win = _windows.parse_window(kwargs) if spec.window else None
        path = spec.path
        if spec.arg:
            path = path.replace("{id}", quote(kwargs.pop(spec.arg), safe=""))
        params: dict = {}
        for flag in spec.flags:
            value = kwargs.get(flag.param)
            if flag.kind == "multi":
                continue  # the CLI's own, not a query param
            if flag.kind == "bool":
                if value:
                    params[flag.param] = "true"
            elif flag.kind == "list":
                if value is not None:
                    params[flag.param] = [v.strip() for v in value.split(",") if v.strip()]
            elif value is not None:
                params[flag.param] = value
        where = aggregate.parse_where(kwargs.get("where")) if "where" in kwargs else []
        if where:
            params.update(spec.where_params)  # e.g. include_details, which the filter reads
        if group_by in _clock.PERIODS:
            # both facts are known now; failing after a full walk would waste it
            if win is None:
                raise click.UsageError(
                    f"--group-by {group_by} needs a window, and {spec.group} records have "
                    "none to divide; group by a field instead."
                )
            if not (win.since or win.mode or spec.default_window):
                raise click.UsageError(
                    f"--group-by {group_by} needs a window to divide: "
                    "pass --since/--until, --today, or --last."
                )
            if not spec.time_field:
                raise click.UsageError(
                    f"{spec.group} records have no timestamp to bucket by {group_by}; "
                    "group by a field instead."
                )
        if spec.prepare is not None:
            params = spec.prepare(params)
        client = deps.connect(ctx)
        window_meta = None
        if win is not None:
            since, until, window_meta = _windows.resolve_window(
                win,
                get_info=lambda: get_clock(ctx, client),
                default_window=spec.default_window,
                note=lambda text: click.echo(text, err=True),
            )
            _windows.apply_window(spec.window, params, since, until)
        if spec.resolve is not None:
            params = spec.resolve(client, params)
        if spec.kind == "raw":
            extra = {"window": window_meta} if window_meta else {}
            if ctx.obj.get("clock"):
                extra.update(_clock.clock_meta(ctx.obj["clock"]))
            _emit_raw(client, spec, path, params, output, extra)
            return
        if count_only and not where:
            n = fetch_count(client, path, params, version=spec.version, unwrap=spec.unwrap)
            if n is None:
                # a bare list or an envelope with no count: walk it and say how far we got
                _records, walked = fetch(
                    client,
                    path,
                    params,
                    paginate=True,
                    limit=limit,
                    version=spec.version,
                    unwrap=spec.unwrap,
                )
                n, pages = walked["total"], walked["pages"]
                # a walk cut short by --limit is a floor too, not just a truncated one
                exact = not walked.get("truncated") and (limit is None or n < limit)
            else:
                pages, exact = 1, True
            records = [{"count": n}]
            meta = {"total": 1, "pages": pages, "count_reported": n, "exact": exact}
        else:
            records, meta = fetch(
                client,
                path,
                params,
                paginate=spec.kind == "list",
                limit=limit,
                version=spec.version,
                unwrap=spec.unwrap,
            )
            fetched = meta["total"]
            if where:
                records, note = aggregate.filter_details(records, where)
                meta["fetched"] = fetched
                meta["total"] = len(records)
                meta["where"] = dict(where)
                meta["note"] = f"{meta['note']} {note}" if meta.get("note") else note
            if count_only:
                # --where made the server count meaningless: count what matched
                n = len(records)
                records = [{"count": n}]
                exact = not meta.get("truncated") and (limit is None or fetched < limit)
                meta = {**meta, "total": 1, "count_reported": n, "exact": exact}
            elif group_by:
                records, meta = _grouped(
                    ctx, client, spec, records, meta, group_by, window_meta, limit, fetched
                )
        if window_meta:
            meta["window"] = window_meta
        if ctx.obj.get("clock"):
            # only when a window flag already fetched it; never an extra request
            meta.update(_clock.clock_meta(ctx.obj["clock"]))
        emit(records, meta, output, fields=fields, fmt=fmt)

    callback.__name__ = f"{spec.group}_{spec.name}".replace("-", "_")
    fn = deps.api_errors(callback)
    fn = click.pass_context(fn)
    for flag in reversed(spec.flags):
        if flag.kind == "bool":
            fn = click.option(*_option_names(flag.param), is_flag=True, help=flag.help)(fn)
        elif flag.kind == "multi":
            fn = click.option(
                *_option_names(flag.param), multiple=True, metavar="KEY=VALUE", help=flag.help
            )(fn)
        else:
            fn = click.option(
                *_option_names(flag.param), type=_TYPES.get(flag.kind, str), help=flag.help
            )(fn)
    if spec.window:
        fn = _windows.window_options(fn)
    if spec.kind == "list":
        fn = click.option(
            *_option_names("group_by"),
            metavar="FIELD|day|week|month",
            help="Count records per value of FIELD (a dotted path), or per site-local period "
            "(needs a window: --since/--until, --today, --last).",
        )(fn)
        fn = click.option(
            *_option_names("count_only"),
            is_flag=True,
            help="Report the server's count for the query in one request; no records.",
        )(fn)
        fn = click.option(
            "--limit", type=click.IntRange(min=1), metavar="N", help="Cap total records returned."
        )(fn)
    fn = click.option(
        "-o",
        "--output",
        type=click.Path(dir_okay=False),
        help="Write the output here instead of stdout.",
    )(fn)
    if spec.kind != "raw":
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
        info = get_clock(ctx, client)
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
