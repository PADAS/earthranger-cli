from er_events_cli.dsl import FieldSpec
from er_events_cli.schema_gen import build_property_pair, choice_field_name


def test_choice_field_name_joins_and_truncates_to_100():
    assert choice_field_name("animal_sighting", "species") == "animal_sighting_species"
    long = choice_field_name("a" * 80, "b" * 80)
    assert len(long) == 100
    assert long == "a" * 80 + "_" + "b" * 19


def test_string_field():
    json_prop, ui = build_property_pair(FieldSpec(key="n", label="Notes", type="string"), "t1")
    assert json_prop == {"type": "string", "title": "Notes"}
    assert ui == {"type": "TEXT", "inputType": "SHORT_TEXT", "parent": "section-1"}


def test_textarea_field():
    _, ui = build_property_pair(FieldSpec(key="n", label="Notes", type="textarea"), "t1")
    assert ui == {"type": "TEXT", "inputType": "LONG_TEXT", "parent": "section-1"}


def test_integer_with_min_max():
    json_prop, ui = build_property_pair(
        FieldSpec(key="c", label="Count", type="integer", min=0, max=500), "t1"
    )
    assert json_prop == {"type": "integer", "title": "Count", "minimum": 0, "maximum": 500}
    assert ui == {"type": "NUMERIC", "parent": "section-1"}


def test_number_boolean_date_datetime():
    json_prop, _ = build_property_pair(FieldSpec(key="r", label="Ratio", type="number"), "t1")
    assert json_prop == {"type": "number", "title": "Ratio"}
    json_prop, ui = build_property_pair(FieldSpec(key="i", label="Injured", type="boolean"), "t1")
    assert json_prop == {"type": "boolean", "title": "Injured"}
    assert ui == {"type": "BOOLEAN", "parent": "section-1"}
    json_prop, ui = build_property_pair(FieldSpec(key="d", label="Seen on", type="date"), "t1")
    assert json_prop == {"type": "string", "format": "date", "title": "Seen on"}
    assert ui == {"type": "DATE_TIME", "parent": "section-1"}
    json_prop, _ = build_property_pair(FieldSpec(key="dt", label="At", type="datetime"), "t1")
    assert json_prop == {"type": "string", "format": "date-time", "title": "At"}
