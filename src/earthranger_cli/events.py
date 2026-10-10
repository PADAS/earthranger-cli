"""Build and post EarthRanger events."""

from __future__ import annotations

import csv
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
        try:
            event["location"] = {"latitude": float(lat_str), "longitude": float(lon_str)}
        except ValueError:
            raise FieldArgError(f"--location expects LAT,LON, got {location!r}") from None
    if title:
        event["title"] = title
    return event


def load_events_file(
    path: str, *, default_event_type: str | None = None, column_map: dict | None = None
) -> list[dict]:
    """Events from a YAML list or, for a `.csv` path, from CSV rows."""
    if path.lower().endswith(".csv"):
        return load_events_csv(path, default_event_type=default_event_type, column_map=column_map)
    with open(path, encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise FieldArgError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(data, list):
        raise FieldArgError("events file must be a YAML list of event objects")
    events: list[dict] = []
    for i, item in enumerate(data):
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


def _cell(row: dict, column: str | None) -> str:
    return row.get(column, "") if column else ""


def _csv_sources(headers: list[str], column_map: dict, path: str) -> dict[str, str | None]:
    """Which column feeds each reserved slot: an explicit map wins, else the
    first header matching the slot's name or an alias."""
    for key, column in column_map.items():
        if column not in headers:
            raise FieldArgError(
                f"--map {key}={column}: no column {column!r} in {path}; "
                f"columns: {', '.join(h for h in headers if h)}"
            )
    lowered = {h.lower(): h for h in headers if h}
    return {
        slot: column_map.get(slot) or next((lowered[n] for n in names if n in lowered), None)
        for slot, names in _RESERVED.items()
    }


def _csv_event(row: dict, n: int, path: str, source: dict, default_event_type: str | None) -> dict:
    event_type = _cell(row, source["event_type"]) or default_event_type
    if not event_type:
        raise FieldArgError(
            f"{path} row {n}: no event_type; add an event_type column or pass --event-type"
        )
    event: dict = {
        "event_type": event_type,
        "time": _cell(row, source["time"]) or datetime.now(UTC).isoformat(),
    }
    if _cell(row, source["title"]):
        event["title"] = _cell(row, source["title"])
    lat, lon = _cell(row, source["lat"]), _cell(row, source["lon"])
    if _cell(row, source["location"]):
        lat, _, lon = _cell(row, source["location"]).partition(",")
    if lat or lon:
        try:
            event["location"] = {"latitude": float(lat), "longitude": float(lon)}
        except ValueError:
            raise FieldArgError(
                f"{path} row {n}: lat/lon must be numbers, got {lat!r}, {lon!r}"
            ) from None
    return event


def load_events_csv(
    path: str, *, default_event_type: str | None = None, column_map: dict | None = None
) -> list[dict]:
    """Events from CSV rows: a fixed column convention so most exports work as
    they are, plus `column_map` (field key -> column name) for the ones that
    do not. Converting a CSV to YAML by hand was the step this removes.

    Row numbers in errors count the header as row 1, so a validation
    message's `events[i]` is row i + 2.
    """
    column_map = dict(column_map or {})
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = [(h or "").strip() for h in (reader.fieldnames or [])]
        if not any(headers):
            raise FieldArgError(f"{path}: no header row")
        source = _csv_sources(headers, column_map, path)
        taken = {c for c in source.values() if c} | set(column_map.values())
        # detail columns in the CSV's own order: a pass-through column keeps its
        # name, a mapped one takes the field key it was mapped to
        key_for_column = {c: k for k, c in column_map.items() if k not in _RESERVED}
        detail_keys = [
            (h, key_for_column.get(h, h))
            for h in headers
            if h and (h not in taken or h in key_for_column)
        ]
        events = []
        for n, raw in enumerate(reader, start=2):
            row = {
                (k or "").strip(): (v.strip() if isinstance(v, str) else "") for k, v in raw.items()
            }
            event = _csv_event(row, n, path, source, default_event_type)
            details: dict = {}
            for column, key in detail_keys:
                if row.get(column, "") != "":
                    details[key] = _scalar(row[column])
            event["event_details"] = details
            events.append(event)
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
