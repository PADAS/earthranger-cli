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
        data = yaml.safe_load(f)
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


def post_events(client, events: list[dict]) -> list[str | None]:
    """Post each event; one entry per event: None on success, error text on failure."""
    outcomes: list[str | None] = []
    for event in events:
        try:
            client.post_event(event)
            outcomes.append(None)
        except Exception as e:  # noqa: BLE001, ERClientException subclasses or transport errors
            outcomes.append(str(e))
    return outcomes
