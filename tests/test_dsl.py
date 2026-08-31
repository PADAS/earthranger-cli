import json

import pytest

from er_events_cli.dsl import OptionSpec, SpecError, load_spec, parse_spec

VALID = {
    "category": {"value": "wildlife_monitoring", "display": "Wildlife Monitoring"},
    "event_types": [
        {
            "value": "animal_sighting",
            "display": "Animal Sighting",
            "icon_id": "mammal_rep",
            "fields": [
                {
                    "key": "species",
                    "label": "Species",
                    "type": "select",
                    "options": [{"value": "elephant", "display": "Elephant"}, "spotted_lion"],
                },
                {"key": "count", "label": "Number of animals", "type": "integer", "min": 0},
                {"key": "notes", "label": "Notes", "type": "textarea"},
            ],
            "required": ["species"],
        }
    ],
}


def test_parse_valid_spec():
    spec = parse_spec(VALID)
    assert spec.category.value == "wildlife_monitoring"
    et = spec.event_types[0]
    assert et.value == "animal_sighting"
    assert et.icon_id == "mammal_rep"
    assert et.is_active is True
    assert et.required == ["species"]
    assert [f.key for f in et.fields] == ["species", "count", "notes"]
    assert et.fields[1].min == 0
    assert et.fields[1].max is None


def test_option_shorthand_derives_value_and_display():
    spec = parse_spec(VALID)
    options = spec.event_types[0].fields[0].options
    assert options[1] == OptionSpec(value="spotted_lion", display="Spotted Lion")


def test_json_file_is_accepted(tmp_path):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(VALID))
    spec = load_spec(str(p))
    assert spec.category.value == "wildlife_monitoring"


def test_yaml_file_loads(tmp_path):
    p = tmp_path / "spec.yaml"
    p.write_text(
        "category: {value: c1, display: C1}\n"
        "event_types:\n"
        "  - value: t1\n"
        "    display: T1\n"
        "    fields:\n"
        "      - {key: notes, label: Notes, type: string}\n"
    )
    spec = load_spec(str(p))
    assert spec.event_types[0].fields[0].type == "string"


def _errors_for(data):
    with pytest.raises(SpecError) as exc:
        parse_spec(data)
    return exc.value.errors


def _spec_with_field(field):
    return {
        "category": {"value": "c1", "display": "C1"},
        "event_types": [
            {"value": "t1", "display": "T1", "fields": [field]},
        ],
    }


def test_missing_category_and_event_types_collected_together():
    errors = _errors_for({})
    assert "category: required mapping with 'value' and 'display'" in errors
    assert "event_types: at least one event type is required" in errors


def test_bad_slug_rejected():
    errors = _errors_for(
        {
            "category": {"value": "Wildlife Monitoring", "display": "X"},
            "event_types": [
                {
                    "value": "t1",
                    "display": "T1",
                    "fields": [{"key": "notes", "label": "N", "type": "string"}],
                }
            ],
        }
    )
    assert "category.value: 'Wildlife Monitoring' must match [a-z0-9_]+" in errors


def test_unknown_type_rejected():
    errors = _errors_for(_spec_with_field({"key": "f1", "label": "F", "type": "selects"}))
    assert any(
        e.startswith("event_types[0].fields[0].type: unsupported type 'selects'") for e in errors
    )


def test_options_required_for_select_and_forbidden_otherwise():
    errors = _errors_for(_spec_with_field({"key": "f1", "label": "F", "type": "select"}))
    assert (
        "event_types[0].fields[0].options: required non-empty list for select/multiselect" in errors
    )
    errors = _errors_for(
        _spec_with_field({"key": "f1", "label": "F", "type": "string", "options": ["a"]})
    )
    assert "event_types[0].fields[0].options: not allowed for type 'string'" in errors


def test_min_only_on_numeric():
    errors = _errors_for(_spec_with_field({"key": "f1", "label": "F", "type": "string", "min": 0}))
    assert "event_types[0].fields[0].min: only allowed on integer/number fields" in errors


def test_required_must_name_declared_field():
    data = _spec_with_field({"key": "f1", "label": "F", "type": "string"})
    data["event_types"][0]["required"] = ["nope"]
    errors = _errors_for(data)
    assert "event_types[0].required: 'nope' is not a declared field key" in errors


def test_duplicates_rejected():
    data = {
        "category": {"value": "c1", "display": "C1"},
        "event_types": [
            {
                "value": "t1",
                "display": "T1",
                "fields": [
                    {"key": "f1", "label": "F", "type": "string"},
                    {"key": "f1", "label": "F2", "type": "string"},
                    {"key": "f2", "label": "F3", "type": "select", "options": ["a", "a"]},
                ],
            },
            {
                "value": "t1",
                "display": "T1 again",
                "fields": [{"key": "g1", "label": "G", "type": "string"}],
            },
        ],
    }
    errors = _errors_for(data)
    assert "event_types[0].fields[1].key: duplicate key 'f1'" in errors
    assert "event_types[0].fields[2].options[1]: duplicate option value 'a'" in errors
    assert "event_types[1].value: duplicate event type value 't1'" in errors


def test_choice_field_name_collision_across_event_types_rejected():
    data = {
        "category": {"value": "c1", "display": "C1"},
        "event_types": [
            {
                "value": "animal",
                "display": "Animal",
                "fields": [
                    {
                        "key": "sighting_species",
                        "label": "Species",
                        "type": "select",
                        "options": ["a"],
                    }
                ],
            },
            {
                "value": "animal_sighting",
                "display": "Animal Sighting",
                "fields": [
                    {"key": "species", "label": "Species", "type": "select", "options": ["a"]}
                ],
            },
        ],
    }
    errors = _errors_for(data)
    assert (
        "event_types[1].fields[0].key: choice field name 'animal_sighting_species' collides "
        "with event_types[0].fields[0].key (choice-list field names are derived from "
        "'<event_type>_<field_key>' and must be unique across the spec)" in errors
    )


def test_long_keys_differing_past_truncation_stay_distinct():
    # With hash-compressed names (ER's field column is varchar(40)), keys that
    # differ only past the readable prefix derive distinct names and parse fine.
    common_prefix = "a" * 97
    data = {
        "category": {"value": "c1", "display": "C1"},
        "event_types": [
            {
                "value": "t1",
                "display": "T1",
                "fields": [
                    {
                        "key": common_prefix + "1",
                        "label": "A",
                        "type": "select",
                        "options": ["a"],
                    },
                    {
                        "key": common_prefix + "2",
                        "label": "B",
                        "type": "select",
                        "options": ["a"],
                    },
                ],
            },
        ],
    }
    spec = parse_spec(data)
    assert len(spec.event_types[0].fields) == 2


def test_distinct_choice_fields_still_parse():
    data = {
        "category": {"value": "c1", "display": "C1"},
        "event_types": [
            {
                "value": "animal",
                "display": "Animal",
                "fields": [
                    {"key": "species", "label": "Species", "type": "select", "options": ["a"]}
                ],
            },
            {
                "value": "plant",
                "display": "Plant",
                "fields": [
                    {
                        "key": "species",
                        "label": "Species",
                        "type": "multiselect",
                        "options": ["a"],
                    }
                ],
            },
        ],
    }
    spec = parse_spec(data)
    assert len(spec.event_types) == 2


def test_load_spec_invalid_yaml_raises_spec_error(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("category: {value: [\n")
    with pytest.raises(SpecError) as exc:
        load_spec(str(p))
    assert len(exc.value.errors) == 1
    assert "invalid YAML" in exc.value.errors[0]
    assert str(p) in exc.value.errors[0]


def test_shipped_example_spec_parses():
    import pathlib

    example = pathlib.Path(__file__).parent.parent / "examples" / "wildlife_monitoring.yaml"
    spec = load_spec(str(example))
    assert spec.category.value == "wildlife_monitoring"
    assert {f.type for f in spec.event_types[0].fields} == {
        "select",
        "integer",
        "number",
        "boolean",
        "date",
        "datetime",
        "multiselect",
        "string",
        "textarea",
        "url",
    }


def test_url_type_accepted():
    spec = parse_spec(
        {
            "category": {"value": "c1", "display": "C1"},
            "event_types": [
                {
                    "value": "t1",
                    "display": "T1",
                    "fields": [{"key": "link", "label": "Link", "type": "url"}],
                }
            ],
        }
    )
    assert spec.event_types[0].fields[0].type == "url"


def test_extra_field_properties_parse():
    spec = parse_spec(
        _spec_with_field(
            {
                "key": "link",
                "label": "Link",
                "type": "string",
                "format": "url",
                "hint": "https://...",
                "description": "A link",
                "default": "https://example.org",
            }
        )
    )
    f = spec.event_types[0].fields[0]
    assert f.format == "url"
    assert f.hint == "https://..."
    assert f.description == "A link"
    assert f.default == "https://example.org"


def test_boolean_default_false_is_kept():
    spec = parse_spec(
        _spec_with_field({"key": "b", "label": "B", "type": "boolean", "default": False})
    )
    assert spec.event_types[0].fields[0].default is False


def test_hint_rules():
    errors = _errors_for(
        _spec_with_field({"key": "b", "label": "B", "type": "boolean", "hint": "x"})
    )
    assert "event_types[0].fields[0].hint: not allowed for type 'boolean'" in errors
    errors = _errors_for(
        _spec_with_field({"key": "s", "label": "S", "type": "string", "hint": "x" * 33})
    )
    assert "event_types[0].fields[0].hint: must be at most 32 characters" in errors


def test_default_type_rules():
    errors = _errors_for(
        _spec_with_field({"key": "s", "label": "S", "type": "string", "default": 3})
    )
    assert "event_types[0].fields[0].default: must be a string for type 'string'" in errors
    errors = _errors_for(
        _spec_with_field({"key": "c", "label": "C", "type": "integer", "default": "many"})
    )
    assert "event_types[0].fields[0].default: must be a number for type 'integer'" in errors
    errors = _errors_for(
        _spec_with_field({"key": "b", "label": "B", "type": "boolean", "default": "yes"})
    )
    assert "event_types[0].fields[0].default: must be true or false for type 'boolean'" in errors
    errors = _errors_for(
        _spec_with_field({"key": "d", "label": "D", "type": "date", "default": "2026-01-01"})
    )
    assert "event_types[0].fields[0].default: not allowed for type 'date'" in errors
    errors = _errors_for(
        _spec_with_field(
            {"key": "x", "label": "X", "type": "select", "options": ["a"], "default": "a"}
        )
    )
    assert "event_types[0].fields[0].default: not allowed for type 'select'" in errors


def test_format_rules():
    errors = _errors_for(
        _spec_with_field({"key": "n", "label": "N", "type": "textarea", "format": "url"})
    )
    assert "event_types[0].fields[0].format: only allowed on string fields" in errors
    errors = _errors_for(
        _spec_with_field({"key": "s", "label": "S", "type": "string", "format": "phone"})
    )
    assert (
        "event_types[0].fields[0].format: unsupported format 'phone' "
        "(supported: email, url, uuid)" in errors
    )
