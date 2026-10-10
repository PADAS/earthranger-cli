import pytest

from conftest import FakeER
from earthranger_cli.events import (
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


def test_load_events_file_invalid_yaml_raises_field_arg_error(tmp_path):
    p = tmp_path / "events.yaml"
    p.write_text("category: {value: [\n")
    with pytest.raises(FieldArgError) as exc:
        load_events_file(str(p))
    assert "invalid YAML" in str(exc.value)
    assert str(p) in str(exc.value)


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


# --- CSV import (#21) ---


def test_load_events_csv_fixed_columns(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text(
        "event_type,time,title,lat,lon,species,count,notes\n"
        "sighting,2026-10-01T10:00:00Z,Two lions,-1.286,36.817,lion,2,\n"
        "sighting,,,,,elephant,true,seen at dusk\n",
        encoding="utf-8",
    )
    events = load_events_file(str(p))
    assert events[0] == {
        "event_type": "sighting",
        "time": "2026-10-01T10:00:00Z",
        "title": "Two lions",
        "location": {"latitude": -1.286, "longitude": 36.817},
        "event_details": {"species": "lion", "count": 2},  # empty notes cell is skipped
    }
    assert events[1]["event_type"] == "sighting"
    assert "location" not in events[1] and "title" not in events[1]
    assert events[1]["event_details"] == {
        "species": "elephant",
        "count": True,
        "notes": "seen at dusk",
    }
    assert "time" in events[1]  # defaulted to now


def test_load_events_csv_default_type_column_map_and_aliases(tmp_path):
    p = tmp_path / "history.csv"
    p.write_text(
        "Timestamp,Species_Name,Latitude,Longitude,Count\n2026-10-01T10:00:00Z,buffalo,-1.5,36.9,4\n",
        encoding="utf-8",
    )
    events = load_events_file(
        str(p),
        default_event_type="sighting",
        column_map={"time": "Timestamp", "species": "Species_Name", "count": "Count"},
    )
    assert events == [
        {
            "event_type": "sighting",
            "time": "2026-10-01T10:00:00Z",
            "location": {"latitude": -1.5, "longitude": 36.9},
            "event_details": {"species": "buffalo", "count": 4},
        }
    ]


def test_load_events_csv_errors_name_the_row(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text("species,lat,lon\nlion,-1.3,36.8\n", encoding="utf-8")
    with pytest.raises(FieldArgError, match="row 2.*event_type.*--event-type"):
        load_events_file(str(p))
    with pytest.raises(FieldArgError, match="--map species=Species: no column 'Species'"):
        load_events_file(str(p), default_event_type="s", column_map={"species": "Species"})
    p.write_text("species,lat,lon\nlion,north,36.8\n", encoding="utf-8")
    with pytest.raises(FieldArgError, match="row 2.*lat.*'north'"):
        load_events_file(str(p), default_event_type="s")
    p.write_text("", encoding="utf-8")
    with pytest.raises(FieldArgError, match="no header row"):
        load_events_file(str(p), default_event_type="s")


def test_load_events_csv_mapped_field_beats_a_same_named_column(tmp_path):
    # review: with --map species=Species_Name, a pass-through `species` column must
    # not overwrite the mapped value whatever the header order
    p = tmp_path / "e.csv"
    for header in ("event_type,Species_Name,species", "event_type,species,Species_Name"):
        cols = header.split(",")
        row = {"event_type": "sighting", "Species_Name": "buffalo", "species": "lion"}
        p.write_text(header + "\n" + ",".join(row[c] for c in cols) + "\n", encoding="utf-8")
        events = load_events_file(str(p), column_map={"species": "Species_Name"})
        assert events[0]["event_details"] == {"species": "buffalo"}, header
    # a reserved column displaced by a map is not smuggled in as a detail either
    p.write_text(
        "event_type,time,Timestamp\nsighting,bogus,2026-10-01T10:00:00Z\n", encoding="utf-8"
    )
    events = load_events_file(str(p), column_map={"time": "Timestamp"})
    assert events[0]["time"] == "2026-10-01T10:00:00Z" and events[0]["event_details"] == {}


def test_load_events_csv_rejects_surplus_cells(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text("event_type,notes\nsighting,first part,discarded part\n", encoding="utf-8")
    with pytest.raises(FieldArgError, match="row 2.*3 cells.*2 columns"):
        load_events_file(str(p))


def test_load_events_csv_reports_an_unparseable_cell(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text("event_type,notes\nsighting,[injured\n", encoding="utf-8")
    with pytest.raises(FieldArgError, match="row 2.*column 'notes'"):
        load_events_file(str(p))


def test_load_events_csv_explicit_detail_map_accepts_reserved_header(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text("event_type,Title\nsighting,Alice\n", encoding="utf-8")

    events = load_events_file(str(p), column_map={"observer": "Title"})

    assert events[0]["event_details"] == {"observer": "Alice"}
    assert events[0]["title"] == "Alice"
