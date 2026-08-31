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
        "with event_types[0].fields[0].key (choice-list field names are "
        "'<event_type>_<field_key>' truncated to 100 chars and must be unique across the spec)"
        in errors
    )


def test_choice_field_name_truncation_collision_rejected():
    common_prefix = "a" * 97
    long_key_a = common_prefix + "1"
    long_key_b = common_prefix + "2"
    data = {
        "category": {"value": "c1", "display": "C1"},
        "event_types": [
            {
                "value": "t1",
                "display": "T1",
                "fields": [
                    {"key": long_key_a, "label": "A", "type": "select", "options": ["a"]},
                    {"key": long_key_b, "label": "B", "type": "select", "options": ["a"]},
                ],
            },
        ],
    }
    errors = _errors_for(data)
    assert any("collides with event_types[0].fields[0].key" in e for e in errors)


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
