"""Build and post EarthRanger events."""

from __future__ import annotations

from datetime import UTC, datetime

import yaml


class FieldArgError(ValueError):
    pass


def parse_field_args(pairs: list[str]) -> dict:
    details: dict = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise FieldArgError(f"--field expects key=value, got {pair!r}")
        details[key] = yaml.safe_load(raw) if raw != "" else ""
    return details


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


def load_events_file(path: str) -> list[dict]:
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
        event = dict(item)
        event.setdefault("time", datetime.now(UTC).isoformat())
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
            outcomes.append(str(e))
    return outcomes
