"""Build and post EarthRanger events."""

from __future__ import annotations

import csv
import re
from datetime import UTC, datetime

import yaml
from erclient.er_errors import ERClientException

from . import client as er


class FieldArgError(ValueError):
    pass


def _scalar(raw: str):
    """A cell or --field value as a YAML scalar: `3` is a number, `true` a boolean."""
    return yaml.safe_load(raw) if raw != "" else ""


def parse_field_args(pairs: list[str]) -> dict:
    details: dict = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise FieldArgError(f"--field expects key=value, got {pair!r}")
        details[key] = _scalar(raw)
    return details


def parse_map_args(pairs: list[str]) -> dict[str, str]:
    """`--map species=Species_Name` -> {"species": "Species_Name"}."""
    mapping: dict[str, str] = {}
    for pair in pairs:
        key, sep, column = pair.partition("=")
        if not sep or not key.strip() or not column.strip():
            raise FieldArgError(f"--map expects key=column, got {pair!r}")
        mapping[key.strip()] = column.strip()
    return mapping


def build_event(
    *,
    event_type: str,
    details: dict,
    location: str | None = None,
    time: str | None = None,
    title: str | None = None,
) -> dict:
    event: dict = {
        "event_type": event_type,
        "time": time or datetime.now(UTC).isoformat(),
        "event_details": details,
    }
    if location:
        lat_str, _, lon_str = location.partition(",")
        event["location"] = _parse_location(lat_str, lon_str, "--location")
    if title:
        event["title"] = title
    return event


def load_events_file(
    path: str, *, default_event_type: str | None = None, column_map: dict | None = None
) -> list[dict]:
    """Events from a YAML list or, for a `.csv` path, from CSV rows.
    `default_event_type` fills in items/rows that name none; `column_map`
    is the CSV `--map` and is refused for a YAML file rather than ignored."""
    if path.lower().endswith(".csv"):
        return load_events_csv(path, default_event_type=default_event_type, column_map=column_map)
    if column_map:
        raise FieldArgError("--map applies to CSV files only")
    with open(path, encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise FieldArgError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(data, list):
        raise FieldArgError("events file must be a YAML list of event objects")
    events: list[dict] = []
    for i, item in enumerate(data):
        if isinstance(item, dict) and "event_type" not in item and default_event_type:
            item = {**item, "event_type": default_event_type}
        if not isinstance(item, dict) or "event_type" not in item:
            raise FieldArgError(f"events[{i}] must be a mapping with an 'event_type'")
        if not isinstance(item["event_type"], str) or not item["event_type"]:
            raise FieldArgError(
                f"events[{i}].event_type must be a non-empty string, got {item['event_type']!r}"
            )
        if "event_details" in item and not isinstance(item["event_details"], dict):
            # das rejects null and any non-object here (the serializer has no
            # allow_null and expects a mapping); refuse before any request,
            # and independently of --no-validate — this is envelope shape,
            # not schema conformance
            raise FieldArgError(
                f"events[{i}].event_details must be a mapping of field values, "
                f"got {item['event_details']!r}"
            )
        event = dict(item)
        event.setdefault("time", datetime.now(UTC).isoformat())
        events.append(event)
    return events


# Reserved CSV columns (matched case-insensitively) and their aliases. Every
# other column is a detail field.
_RESERVED = {
    "event_type": ("event_type",),
    "time": ("time",),
    "title": ("title",),
    "location": ("location",),
    "lat": ("lat", "latitude"),
    "lon": ("lon", "lng", "longitude"),
}
_RESERVED_NAMES = {name for names in _RESERVED.values() for name in names}
_SLOT_OF = {name: slot for slot, names in _RESERVED.items() for name in names}
_INT_RE = re.compile(r"^[+-]?(0|[1-9]\d*)$")  # 0123 is an identifier, not a number
# a decimal needs a point or an exponent; "0123" and "42" are the integer rule's business
_FLOAT_RE = re.compile(r"^[+-]?((\d+\.\d*|\.\d+)([eE][+-]?\d+)?|\d+[eE][+-]?\d+)$")
_DELIMITERS = ",;\t|"


def _coerce(text: str):
    """A CSV cell as a value: integers, decimals and true/false become those,
    everything else stays text. Spreadsheet exports are full of values that a
    YAML reader would mangle (dates, times, 0123, NO, #12, "a: b"), so unlike
    --field this never uses YAML."""
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text):
        return float(text)
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    return text


def _parse_location(lat, lon, where: str) -> dict:
    """{latitude, longitude} from two cells or the halves of LAT,LON; a missing
    or non-numeric half is named."""
    lat, lon = str(lat).strip(), str(lon).strip()
    if not lat or not lon:
        missing = "lat" if not lat else "lon"
        raise FieldArgError(f"{where}: {missing} is missing (got lat {lat!r}, lon {lon!r})")
    try:
        return {"latitude": float(lat), "longitude": float(lon)}
    except ValueError:
        raise FieldArgError(f"{where}: lat/lon must be numbers, got {lat!r}, {lon!r}") from None


def _cell(row: dict, column: str | None) -> str:
    return row.get(column, "") if column else ""


def _sniff_delimiter(header_line: str) -> str:
    """The delimiter the header uses most (comma, semicolon, tab or pipe):
    Excel in many locales writes semicolons."""
    counts = {d: header_line.count(d) for d in _DELIMITERS}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def _csv_plan(headers: list[str], column_map: dict, path: str) -> dict:
    """How columns feed the event: which column fills each reserved slot and
    which columns become which detail keys, with every ambiguity refused up
    front so row order and column order can never decide the result."""
    seen: dict[str, str] = {}
    for h in headers:
        if h and h.lower() in seen:
            raise FieldArgError(f"{path}: duplicate column {h!r} (also {seen[h.lower()]!r})")
        if h:
            seen[h.lower()] = h
    # map keys naming a reserved slot (or alias) are case-insensitive, like headers
    norm_map: dict[str, str] = {}
    for key, column in column_map.items():
        if column not in headers:
            raise FieldArgError(
                f"--map {key}={column}: no column {column!r} in {path}; "
                f"columns: {', '.join(h for h in headers if h)}"
            )
        norm_map[_SLOT_OF.get(key.lower(), key)] = column
    targets: dict[str, str] = {}
    for key, column in norm_map.items():
        if column in targets:
            raise FieldArgError(
                f"--map: column {column!r} is mapped twice ({targets[column]} and {key})"
            )
        targets[column] = key
    detail_map = {c: k for k, c in norm_map.items() if k not in _RESERVED}
    lowered = {h.lower(): h for h in headers if h}
    source: dict[str, str | None] = {}
    for slot, names in _RESERVED.items():
        # an explicit map wins; else the first header matching the slot's name or
        # an alias. A detail map onto such a column adds the detail; the column
        # still fills its slot (a `Title` column is the title *and* the observer).
        source[slot] = norm_map.get(slot) or next((lowered[n] for n in names if n in lowered), None)
    taken = {c for c in source.values() if c} | set(norm_map.values())
    reserved_like = {h for h in headers if h.lower() in _RESERVED_NAMES}
    detail_keys = [
        (h, detail_map.get(h, h))
        for h in headers
        if h
        and (h in detail_map or (h not in taken and h not in reserved_like and h not in norm_map))
    ]
    return {"source": source, "detail_keys": detail_keys, "width": len(headers)}


def _csv_event(row: dict, where: str, source: dict, default_event_type: str | None) -> dict:
    event_type = _cell(row, source["event_type"]) or default_event_type
    if not event_type:
        raise FieldArgError(
            f"{where}: no event_type; add an event_type column or pass --event-type"
        )
    event: dict = {
        "event_type": event_type,
        "time": _cell(row, source["time"]) or datetime.now(UTC).isoformat(),
    }
    if _cell(row, source["title"]):
        event["title"] = _cell(row, source["title"])
    loc = _cell(row, source["location"])
    lat, lon = _cell(row, source["lat"]), _cell(row, source["lon"])
    if loc and (lat or lon):
        raise FieldArgError(f"{where}: both a location cell and lat/lon cells are filled; use one")
    if loc:
        lat, _, lon = loc.partition(",")
    if lat or lon:
        event["location"] = _parse_location(lat, lon, where)
    return event


def load_events_csv(
    path: str, *, default_event_type: str | None = None, column_map: dict | None = None
) -> list[dict]:
    """Events from CSV rows: a fixed column convention so most exports work as
    they are, plus `column_map` (field key -> column name) for the ones that
    do not. Converting a CSV to YAML by hand was the step this removes.

    Row numbers in errors are the file's physical line numbers (the header is
    line 1), so they match a spreadsheet's row numbers even past blank lines
    and quoted multi-line cells.
    """
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            text = f.read()
    except UnicodeDecodeError as e:
        raise FieldArgError(
            f"{path} is not valid UTF-8 ({e.reason} at byte {e.start}); save it as UTF-8"
        ) from None
    header_line = text.split("\n", 1)[0]
    reader = csv.DictReader(text.splitlines(keepends=True), delimiter=_sniff_delimiter(header_line))
    try:
        headers = [(h or "").strip() for h in (reader.fieldnames or [])]
        if not any(headers):
            raise FieldArgError(f"{path}: no header row")
        plan = _csv_plan(headers, dict(column_map or {}), path)
        source, detail_keys, width = plan["source"], plan["detail_keys"], plan["width"]
        events = []
        for raw in reader:
            where = f"{path} row {reader.line_num}"
            surplus = raw.get(None)  # DictReader parks cells beyond the header here
            if surplus:
                raise FieldArgError(
                    f"{where}: {width + len(surplus)} cells for {width} columns; "
                    "quote a cell that contains the delimiter"
                )
            row = {
                (k or "").strip(): (v.strip() if isinstance(v, str) else "")
                for k, v in raw.items()
                if k is not None
            }
            event = _csv_event(row, where, source, default_event_type)
            event["event_details"] = {
                key: _coerce(row[column])
                for column, key in detail_keys
                if row.get(column, "") != ""
            }
            events.append(event)
    except csv.Error as e:
        raise FieldArgError(f"{path} line {reader.line_num}: {e}") from None
    return events


class PostAborted(Exception):
    """A batch stopped early on a rejected credential (401).

    Carries what happened before the stop so the CLI can report which events
    were already created (and must not be re-posted) before it explains the
    credential problem: `outcomes` covers the events attempted before the
    failing one, `failed` is the event the 401 came back for, `remaining`
    counts the events never attempted, and `cause` is the erclient error.
    """

    def __init__(self, cause: Exception, outcomes: list[str | None], failed: dict, remaining: int):
        super().__init__(str(cause))
        self.cause = cause
        self.outcomes = outcomes
        self.failed = failed
        self.remaining = remaining


def post_events(client, events: list[dict]) -> list[str | None]:
    """Post each event; one entry per event: None on success, error text on failure.

    A rejected credential (401) aborts the batch — it would fail every
    remaining event identically — via PostAborted, which carries the partial
    outcomes so nothing already created goes unreported.
    """
    outcomes: list[str | None] = []
    for i, event in enumerate(events):
        try:
            client.post_event(event)
            outcomes.append(None)
        except Exception as e:  # ERClientException subclasses or transport errors
            if getattr(e, "status_code", None) == 401:
                raise PostAborted(e, outcomes, event, len(events) - i - 1) from e
            outcomes.append(er.describe_error(e) if isinstance(e, ERClientException) else str(e))
    return outcomes
