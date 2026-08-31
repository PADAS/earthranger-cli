from er_events_cli.dsl import EventTypeSpec, FieldSpec, OptionSpec
from er_events_cli.schema_gen import (
    build_event_type_payload,
    build_property_pair,
    build_schema,
    choice_field_name,
)


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


REF = "/api/v2.0/schemas/choices.json?field=t1_species"


def _select_field(type_="select"):
    return FieldSpec(
        key="species", label="Species", type=type_,
        options=[OptionSpec("elephant", "Elephant")],
    )


def test_select_field():
    json_prop, ui = build_property_pair(_select_field(), "t1")
    assert json_prop == {"type": "string", "title": "Species", "anyOf": [{"$ref": REF}]}
    assert ui == {
        "type": "CHOICE_LIST",
        "inputType": "DROPDOWN",
        "placeholder": "",
        "choices": {
            "type": "EXISTING_CHOICE_LIST",
            "existingChoiceList": ["t1_species"],
            "eventTypeCategories": [],
            "featureCategories": [],
            "myDataType": "",
            "subjectGroups": [],
            "subjectSubtypes": [],
        },
        "parent": "section-1",
    }


def test_multiselect_field():
    json_prop, _ = build_property_pair(_select_field("multiselect"), "t1")
    assert json_prop == {
        "type": "array",
        "title": "Species",
        "uniqueItems": True,
        "items": {"type": "string", "anyOf": [{"$ref": REF}]},
    }


def _event_type():
    return EventTypeSpec(
        value="t1", display="T One",
        fields=[_select_field(), FieldSpec(key="notes", label="Notes", type="string")],
        required=["species"],
    )


def test_build_schema_envelope():
    schema = build_schema(_event_type())
    assert schema["json"]["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["json"]["type"] == "object"
    assert schema["json"]["unevaluatedProperties"] is False
    assert schema["json"]["required"] == ["species"]
    assert list(schema["json"]["properties"]) == ["species", "notes"]
    section = schema["ui"]["sections"]["section-1"]
    assert section["label"] == "Details"
    assert section["leftColumn"] == [
        {"name": "species", "type": "field"},
        {"name": "notes", "type": "field"},
    ]
    assert schema["ui"]["order"] == ["section-1"]
    assert schema["ui"]["headers"] == {}
    assert list(schema["ui"]["fields"]) == ["species", "notes"]


def test_build_event_type_payload():
    payload = build_event_type_payload(_event_type(), "wildlife_monitoring")
    assert payload["value"] == "t1"
    assert payload["display"] == "T One"
    assert payload["category"] == "wildlife_monitoring"
    assert payload["is_active"] is True
    assert payload["readonly"] is False
    assert payload["schema"] == build_schema(_event_type())
    assert "icon_id" not in payload
    et = _event_type()
    et.icon_id = "mammal_rep"
    assert build_event_type_payload(et, "c")["icon_id"] == "mammal_rep"
