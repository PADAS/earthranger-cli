from er_events_cli.dsl import EventTypeSpec, FieldSpec, OptionSpec
from er_events_cli.schema_gen import (
    build_event_type_payload,
    build_property_pair,
    build_schema,
    choice_field_name,
)


def test_choice_field_name_fits_er_varchar_40():
    # ER's Choice.field column is varchar(40) (das/choices/models.py); a longer
    # name 500s on POST: "value too long for type character varying(40)".
    name = choice_field_name("addaxai_connect_detection", "addaxai_connect_species")
    assert len(name) <= 40


def test_choice_field_name_short_names_unchanged():
    assert choice_field_name("animal_sighting", "species") == "animal_sighting_species"


def test_choice_field_name_long_names_deterministic_and_distinct():
    a1 = choice_field_name("addaxai_connect_detection", "addaxai_connect_species")
    a2 = choice_field_name("addaxai_connect_detection", "addaxai_connect_species")
    assert a1 == a2
    assert a1.startswith("addaxai_connect_detection_adda")
    # names that differ only past the truncation point stay distinct
    b = choice_field_name("a" * 30, "b" * 30)
    c = choice_field_name("a" * 30, "b" * 29 + "c")
    assert b != c
    assert len(b) == 40 and len(c) == 40


def test_string_field():
    json_prop, ui = build_property_pair(FieldSpec(key="n", label="Notes", type="string"), "t1")
    assert json_prop == {"type": "string", "title": "Notes", "deprecated": False}
    assert ui == {"type": "TEXT", "inputType": "SHORT_TEXT", "parent": "section-1"}


def test_textarea_field():
    _, ui = build_property_pair(FieldSpec(key="n", label="Notes", type="textarea"), "t1")
    assert ui == {"type": "TEXT", "inputType": "LONG_TEXT", "parent": "section-1"}


def test_integer_with_min_max():
    json_prop, ui = build_property_pair(
        FieldSpec(key="c", label="Count", type="integer", min=0, max=500), "t1"
    )
    # ER's meta-schema has no integer variant; integer is advisory and emits number.
    assert json_prop == {
        "type": "number",
        "title": "Count",
        "deprecated": False,
        "minimum": 0,
        "maximum": 500,
    }
    assert ui == {"type": "NUMERIC", "parent": "section-1"}


def test_number_boolean_date_datetime():
    json_prop, _ = build_property_pair(FieldSpec(key="r", label="Ratio", type="number"), "t1")
    assert json_prop == {"type": "number", "title": "Ratio", "deprecated": False}
    json_prop, ui = build_property_pair(FieldSpec(key="i", label="Injured", type="boolean"), "t1")
    assert json_prop == {"type": "boolean", "title": "Injured", "deprecated": False}
    assert ui == {"type": "BOOLEAN", "parent": "section-1"}
    json_prop, ui = build_property_pair(FieldSpec(key="d", label="Seen on", type="date"), "t1")
    assert json_prop == {
        "type": "string",
        "format": "date",
        "title": "Seen on",
        "deprecated": False,
    }
    assert ui == {"type": "DATE_TIME", "parent": "section-1"}
    json_prop, _ = build_property_pair(FieldSpec(key="dt", label="At", type="datetime"), "t1")
    assert json_prop == {
        "type": "string",
        "format": "date-time",
        "title": "At",
        "deprecated": False,
    }


def test_url_field():
    json_prop, ui = build_property_pair(
        FieldSpec(key="link", label="Source link", type="url"), "t1"
    )
    assert json_prop == {
        "type": "string",
        "format": "uri",
        "title": "Source link",
        "deprecated": False,
    }
    assert ui == {"type": "TEXT", "inputType": "SHORT_TEXT", "parent": "section-1"}


REF = "/api/v2.0/schemas/choices.json?field=t1_species"


def _select_field(type_="select"):
    return FieldSpec(
        key="species",
        label="Species",
        type=type_,
        options=[OptionSpec("elephant", "Elephant")],
    )


def test_select_field():
    json_prop, ui = build_property_pair(_select_field(), "t1")
    assert json_prop == {
        "type": "string",
        "title": "Species",
        "deprecated": False,
        "anyOf": [{"$ref": REF}],
    }
    # ER removed the builder-only "choices" block from its meta-schema
    # (additionalProperties: False now rejects it); the $ref is the linkage.
    assert ui == {"type": "CHOICE_LIST", "inputType": "DROPDOWN", "parent": "section-1"}


def test_multiselect_field():
    json_prop, _ = build_property_pair(_select_field("multiselect"), "t1")
    assert json_prop == {
        "type": "array",
        "title": "Species",
        "deprecated": False,
        "uniqueItems": True,
        "items": {"type": "string", "anyOf": [{"$ref": REF}]},
    }


def _event_type():
    return EventTypeSpec(
        value="t1",
        display="T One",
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
    assert "icon" not in payload
    et = _event_type()
    et.icon_id = "mammal_rep"
    # ER's v2 API ignores a top-level icon_id (read-only, derived); the
    # writable model field is "icon".
    payload = build_event_type_payload(et, "c")
    assert payload["icon"] == "mammal_rep"
    assert "icon_id" not in payload


def test_extra_properties_on_string_field():
    json_prop, ui = build_property_pair(
        FieldSpec(
            key="link",
            label="Link",
            type="string",
            format="url",
            hint="https://...",
            description="A link",
            default="https://example.org",
        ),
        "t1",
    )
    assert json_prop == {
        "type": "string",
        "title": "Link",
        "deprecated": False,
        "format": "uri",  # DSL "url" maps to JSON Schema "uri"
        "description": "A link",
        "default": "https://example.org",
    }
    assert ui == {
        "type": "TEXT",
        "inputType": "SHORT_TEXT",
        "parent": "section-1",
        "placeholder": "https://...",
    }


def test_boolean_default_false_emitted():
    json_prop, _ = build_property_pair(
        FieldSpec(key="b", label="B", type="boolean", default=False), "t1"
    )
    assert json_prop["default"] is False


def test_select_hint_and_description():
    f = _select_field()
    f.hint = "pick one"
    f.description = "The species"
    json_prop, ui = build_property_pair(f, "t1")
    assert json_prop["description"] == "The species"
    assert "default" not in json_prop
    assert ui["placeholder"] == "pick one"


def test_two_column_layout_section():
    from er_events_cli.dsl import LayoutSpec

    et = EventTypeSpec(
        value="t1",
        display="T1",
        layout=LayoutSpec(label="", columns=2),
        fields=[
            FieldSpec(key="a", label="A", type="string"),
            FieldSpec(key="b", label="B", type="string", column="right"),
            FieldSpec(key="c", label="C", type="string"),
        ],
    )
    section = build_schema(et)["ui"]["sections"]["section-1"]
    assert section["label"] == ""
    assert section["columns"] == 2
    assert section["leftColumn"] == [
        {"name": "a", "type": "field"},
        {"name": "c", "type": "field"},
    ]
    assert section["rightColumn"] == [{"name": "b", "type": "field"}]


def test_explicit_choices_field_used_in_ref():
    f = _select_field()
    f.choices_field = "illegal_fishing_action"
    json_prop, _ = build_property_pair(f, "t1")
    assert json_prop["anyOf"] == [
        {"$ref": "/api/v2.0/schemas/choices.json?field=illegal_fishing_action"}
    ]
