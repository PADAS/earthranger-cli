import pytest

from conftest import FakeER
from er_events_cli.events import (
    FieldArgError,
    build_event,
    load_events_file,
    parse_field_args,
    post_events,
)


def test_parse_field_args_yaml_coercion():
    details = parse_field_args(["species=elephant", "count=3", "injured=true", "threats=[a, b]"])
    assert details == {"species": "elephant", "count": 3, "injured": True, "threats": ["a", "b"]}


def test_parse_field_args_rejects_missing_equals():
    with pytest.raises(FieldArgError):
        parse_field_args(["species"])


def test_build_event_minimal_defaults_time():
    event = build_event(event_type="sighting", details={"species": "elephant"})
    assert event["event_type"] == "sighting"
    assert event["event_details"] == {"species": "elephant"}
    assert "time" in event
    assert "location" not in event


def test_build_event_with_location_time_title():
    event = build_event(
        event_type="sighting",
        details={},
        location="-1.286,36.817",
        time="2026-08-31T12:00:00Z",
        title="Morning patrol",
    )
    assert event["location"] == {"latitude": -1.286, "longitude": 36.817}
    assert event["time"] == "2026-08-31T12:00:00Z"
    assert event["title"] == "Morning patrol"


def test_build_event_bad_location():
    with pytest.raises(FieldArgError):
        build_event(event_type="s", details={}, location="nowhere")


def test_load_events_file(tmp_path):
    p = tmp_path / "events.yaml"
    p.write_text(
        "- event_type: sighting\n"
        "  event_details: {species: elephant}\n"
        "  location: {latitude: -1.0, longitude: 36.0}\n"
        "- event_type: sighting\n"
        "  event_details: {species: lion}\n"
    )
    events = load_events_file(str(p))
    assert len(events) == 2
    assert events[0]["location"] == {"latitude": -1.0, "longitude": 36.0}
    assert all("time" in e for e in events)


def test_load_events_file_rejects_non_list(tmp_path):
    p = tmp_path / "events.yaml"
    p.write_text("event_type: sighting\n")
    with pytest.raises(FieldArgError):
        load_events_file(str(p))


def test_post_events_reports_per_event_outcomes():
    fake = FakeER()

    def failing_post(event):
        if event["event_details"]["species"] == "lion":
            raise RuntimeError("400 bad species")
        return event

    fake.post_event = failing_post
    events = [
        {"event_type": "s", "event_details": {"species": "elephant"}},
        {"event_type": "s", "event_details": {"species": "lion"}},
    ]
    outcomes = post_events(fake, events)
    assert outcomes[0] is None
    assert "400 bad species" in outcomes[1]
