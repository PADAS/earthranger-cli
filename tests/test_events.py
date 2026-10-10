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


def test_load_events_csv_keeps_yaml_looking_text_as_text(tmp_path):
    # cells never go through a YAML reader, so "[injured" is just the note it is
    p = tmp_path / "e.csv"
    p.write_text("event_type,notes\nsighting,[injured\n", encoding="utf-8")
    assert load_events_file(str(p))[0]["event_details"] == {"notes": "[injured"}


def test_load_events_csv_explicit_detail_map_accepts_reserved_header(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text("event_type,Title\nsighting,Alice\n", encoding="utf-8")

    events = load_events_file(str(p), column_map={"observer": "Title"})

    assert events[0]["event_details"] == {"observer": "Alice"}
    assert events[0]["title"] == "Alice"


# --- review round on the CSV import ---


def _csv(tmp_path, text, name="e.csv", encoding="utf-8"):
    p = tmp_path / name
    p.write_bytes(text.encode(encoding) if isinstance(text, str) else text)
    return str(p)


def test_csv_cells_coerce_only_numbers_and_booleans(tmp_path):
    path = _csv(
        tmp_path,
        "event_type,date,when,collar,country,ref,note,count,ratio,flag\n"
        "s,2026-10-01,12:30,0123,NO,#12,Notes: injured,3,-0.5,TRUE\n",
    )
    details = load_events_file(path)[0]["event_details"]
    assert details == {
        "date": "2026-10-01",
        "when": "12:30",
        "collar": "0123",  # a leading zero is an identifier, not octal
        "country": "NO",
        "ref": "#12",
        "note": "Notes: injured",
        "count": 3,
        "ratio": -0.5,
        "flag": True,
    }


def test_csv_decode_and_parse_errors_are_clean(tmp_path):
    path = _csv(tmp_path, "event_type,species\ns,Olé\n", encoding="cp1252")
    with pytest.raises(FieldArgError, match="not valid UTF-8"):
        load_events_file(path)
    # (csv.Error is wrapped the same way; since Python 3.11 the reader accepts
    # NUL bytes, so there is no portable input that triggers it to pin here)


def test_csv_row_numbers_follow_physical_lines(tmp_path):
    path = _csv(
        tmp_path,
        'event_type,notes,lat,lon\n\ns,"two\nlines",-1.3,36.8\ns,x,north,36.8\n',
    )
    with pytest.raises(
        FieldArgError, match="row 5"
    ):  # header 1, blank 2, two-line row 3-4, bad row 5
        load_events_file(path)


def test_yaml_file_rejects_map_and_takes_default_type(tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text("- event_details: {a: 1}\n- event_type: b\n", encoding="utf-8")
    with pytest.raises(FieldArgError, match="--map applies to CSV files only"):
        load_events_file(str(p), column_map={"a": "A"})
    events = load_events_file(str(p), default_event_type="dflt")
    assert [e["event_type"] for e in events] == ["dflt", "b"]
    with pytest.raises(FieldArgError, match="events\\[0\\].*event_type"):
        load_events_file(str(p))


def test_csv_map_keys_for_reserved_slots_are_case_insensitive(tmp_path):
    path = _csv(tmp_path, "event_type,Y_Coord,X_Coord\ns,-1.3,36.8\n")
    events = load_events_file(path, column_map={"Latitude": "Y_Coord", "Longitude": "X_Coord"})
    assert events[0]["location"] == {"latitude": -1.3, "longitude": 36.8}
    assert events[0]["event_details"] == {}


def test_csv_map_collisions_and_opt_out(tmp_path):
    path = _csv(tmp_path, "event_type,Name\ns,buffalo\n")
    with pytest.raises(FieldArgError, match="--map.*Name.*twice"):
        load_events_file(path, column_map={"species": "Name", "common_name": "Name"})
    # a detail map onto a reserved-named column adds the detail; the column
    # still fills its slot (see test_load_events_csv_explicit_detail_map_accepts_reserved_header)
    path = _csv(tmp_path, "event_type,Title\ns,Ann\n")
    events = load_events_file(path, column_map={"observer": "Title"})
    assert events[0]["title"] == "Ann" and events[0]["event_details"] == {"observer": "Ann"}


def test_csv_location_conflicts_and_half_pairs(tmp_path):
    path = _csv(tmp_path, 'event_type,location,lat,lon\ns,"-1.3,36.8",-1.4,36.9\n')
    with pytest.raises(FieldArgError, match="row 2.*both"):
        load_events_file(path)
    path = _csv(tmp_path, "event_type,lat,lon\ns,-1.3,\n")
    with pytest.raises(FieldArgError, match="row 2.*lon is missing"):
        load_events_file(path)


def test_csv_duplicate_headers_are_rejected(tmp_path):
    path = _csv(tmp_path, "event_type,Lat,lat,lon\ns,,-1.3,36.8\n")
    with pytest.raises(FieldArgError, match="duplicate column.*lat"):
        load_events_file(path)


def test_csv_delimiter_is_sniffed(tmp_path):
    path = _csv(tmp_path, "event_type;lat;lon;species\ns;-1.3;36.8;lion\n")
    events = load_events_file(path)
    assert events[0]["location"] == {"latitude": -1.3, "longitude": 36.8}
    assert events[0]["event_details"] == {"species": "lion"}
    path = _csv(tmp_path, "event_type\tspecies\ns\tlion\n")
    assert load_events_file(path)[0]["event_details"] == {"species": "lion"}


def test_build_event_location_errors_name_the_missing_half():
    with pytest.raises(FieldArgError, match="lon is missing"):
        build_event(event_type="s", details={}, location="-1.3")
