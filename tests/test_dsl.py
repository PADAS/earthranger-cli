import json

import pytest

from earthranger_cli.dsl import OptionSpec, SpecError, load_spec, parse_spec

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
    assert "event_types[0].fields[0].options: required list for select/multiselect" in errors
    errors = _errors_for(
        _spec_with_field({"key": "f1", "label": "F", "type": "string", "options": ["a"]})
    )
    assert "event_types[0].fields[0].options: not allowed for type 'string'" in errors


def test_empty_options_list_is_valid():
    # an all-inactive choice set round-trips to `options: []` (pull); parsing
    # must accept it rather than requiring at least one option.
    spec = parse_spec(
        _spec_with_field({"key": "f1", "label": "F", "type": "select", "options": []})
    )
    assert spec.event_types[0].fields[0].options == []


def test_min_only_on_numeric():
    errors = _errors_for(_spec_with_field({"key": "f1", "label": "F", "type": "string", "min": 0}))
    assert (
        "event_types[0].fields[0].min: only allowed on integer/number/collection fields" in errors
    )


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


def test_layout_defaults():
    spec = parse_spec(_spec_with_field({"key": "n", "label": "N", "type": "string"}))
    et = spec.event_types[0]
    assert (et.layout.label, et.layout.columns) == ("Details", 1)
    assert et.fields[0].column == "left"


def test_layout_two_columns_parses():
    data = _spec_with_field({"key": "n", "label": "N", "type": "string", "column": "right"})
    data["event_types"][0]["layout"] = {"label": "", "columns": 2}
    spec = parse_spec(data)
    et = spec.event_types[0]
    assert (et.layout.label, et.layout.columns) == ("", 2)
    assert et.fields[0].column == "right"


def test_layout_validation():
    data = _spec_with_field({"key": "n", "label": "N", "type": "string"})
    data["event_types"][0]["layout"] = {"columns": 3}
    errors = _errors_for(data)
    assert "event_types[0].layout.columns: must be 1 or 2" in errors

    errors = _errors_for(
        _spec_with_field({"key": "n", "label": "N", "type": "string", "column": "middle"})
    )
    assert "event_types[0].fields[0].column: must be 'left' or 'right'" in errors

    errors = _errors_for(
        _spec_with_field({"key": "n", "label": "N", "type": "string", "column": "right"})
    )
    assert "event_types[0].fields[0].column: 'right' requires layout columns: 2" in errors


def test_choices_field_parses_and_validates():
    spec = parse_spec(
        _spec_with_field(
            {
                "key": "action",
                "label": "Action",
                "type": "select",
                "choices_field": "illegal_fishing_action",
                "options": ["stopped"],
            }
        )
    )
    assert spec.event_types[0].fields[0].choices_field == "illegal_fishing_action"

    errors = _errors_for(
        _spec_with_field({"key": "n", "label": "N", "type": "string", "choices_field": "x"})
    )
    assert "event_types[0].fields[0].choices_field: only allowed on select/multiselect" in errors

    errors = _errors_for(
        _spec_with_field(
            {
                "key": "s",
                "label": "S",
                "type": "select",
                "options": ["a"],
                "choices_field": "bad name!",
            }
        )
    )
    assert any("choices_field: must match" in e for e in errors)

    errors = _errors_for(
        _spec_with_field(
            {
                "key": "s",
                "label": "S",
                "type": "select",
                "options": ["a"],
                "choices_field": "x" * 41,
            }
        )
    )
    assert any("choices_field: must match" in e for e in errors)


def _two_fields_sharing(name, options_a, options_b):
    return {
        "category": {"value": "c1", "display": "C1"},
        "event_types": [
            {
                "value": "t1",
                "display": "T1",
                "fields": [
                    {
                        "key": "a",
                        "label": "A",
                        "type": "select",
                        "choices_field": name,
                        "options": options_a,
                    }
                ],
            },
            {
                "value": "t2",
                "display": "T2",
                "fields": [
                    {
                        "key": "b",
                        "label": "B",
                        "type": "select",
                        "choices_field": name,
                        "options": options_b,
                    }
                ],
            },
        ],
    }


def test_shared_choices_field_allowed_when_options_identical():
    spec = parse_spec(_two_fields_sharing("shared_actions", ["stop", "go"], ["stop", "go"]))
    assert len(spec.event_types) == 2


def test_shared_choices_field_rejected_when_options_differ():
    errors = _errors_for(_two_fields_sharing("shared_actions", ["stop"], ["go"]))
    assert any("shared_actions" in e and "identical options" in e for e in errors)


def _implicit_and_explicit_sharing(options_a, options_b):
    # type "a" field "species" derives the name "a_species" implicitly; type
    # "b" reuses that same name explicitly via choices_field.
    return {
        "category": {"value": "c1", "display": "C1"},
        "event_types": [
            {
                "value": "a",
                "display": "A",
                "fields": [
                    {"key": "species", "label": "Species", "type": "select", "options": options_a}
                ],
            },
            {
                "value": "b",
                "display": "B",
                "fields": [
                    {
                        "key": "other",
                        "label": "Other",
                        "type": "select",
                        "choices_field": "a_species",
                        "options": options_b,
                    }
                ],
            },
        ],
    }


def test_implicit_and_explicit_choice_field_sharing_allowed_when_options_identical():
    spec = parse_spec(_implicit_and_explicit_sharing(["x", "y"], ["x", "y"]))
    assert len(spec.event_types) == 2


def test_implicit_and_explicit_choice_field_sharing_rejected_when_options_differ():
    errors = _errors_for(_implicit_and_explicit_sharing(["x", "y"], ["x"]))
    assert any("a_species" in e and "identical options" in e for e in errors)


def test_two_implicit_fields_colliding_still_rejected_even_with_identical_options():
    # the corruption guard for accidental derivation collisions stays even
    # when options happen to match.
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
    assert any("collides with" in e for e in errors)


def test_field_keys_accept_er_charset():
    # das FORM_ELEMENT_SEGMENT_PATTERN is [a-zA-Z0-9_-]+ — stock types use
    # hyphens and uppercase (cameratraprep_camera-name, mistrep_Method).
    spec = parse_spec(
        _spec_with_field({"key": "cameratraprep_camera-name", "label": "N", "type": "string"})
    )
    assert spec.event_types[0].fields[0].key == "cameratraprep_camera-name"
    errors = _errors_for(_spec_with_field({"key": "bad key!", "label": "N", "type": "string"}))
    assert any("must match [a-zA-Z0-9_-]+" in e for e in errors)


def test_option_values_are_free_text():
    spec = parse_spec(
        _spec_with_field(
            {"key": "s", "label": "S", "type": "select", "options": ["Apprehend", "Chase"]}
        )
    )
    opts = spec.event_types[0].fields[0].options
    assert [o.value for o in opts] == ["Apprehend", "Chase"]
    assert [o.display for o in opts] == ["Apprehend", "Chase"]


SECTIONED = {
    "category": {"value": "c1", "display": "C1"},
    "event_types": [
        {
            "value": "entry_alert",
            "display": "Entry Alert",
            "sections": [
                {"label": "", "fields": [{"key": "at", "label": "Time", "type": "datetime"}]},
                {
                    "label": "",
                    "columns": 2,
                    "fields": [
                        {"key": "lat", "label": "Lat", "type": "number"},
                        {"key": "lon", "label": "Lon", "type": "number", "column": "right"},
                    ],
                },
            ],
            "required": ["at", "lat"],
        }
    ],
}


def test_sections_parse():
    import copy

    spec = parse_spec(copy.deepcopy(SECTIONED))
    et = spec.event_types[0]
    assert len(et.sections) == 2
    assert (et.sections[0].label, et.sections[0].columns) == ("", 1)
    assert (et.sections[1].label, et.sections[1].columns) == ("", 2)
    assert [f.key for f in et.sections[1].fields] == ["lat", "lon"]
    # flat view spans all sections (used by choices/required/collision checks)
    assert [f.key for f in et.fields] == ["at", "lat", "lon"]
    assert et.required == ["at", "lat"]


def test_single_section_sugar_still_works():
    spec = parse_spec(_spec_with_field({"key": "n", "label": "N", "type": "string"}))
    et = spec.event_types[0]
    assert len(et.sections) == 1
    assert (et.sections[0].label, et.sections[0].columns) == ("Details", 1)
    assert [f.key for f in et.sections[0].fields] == ["n"]


def test_sections_mutually_exclusive_with_fields_and_layout():
    import copy

    data = copy.deepcopy(SECTIONED)
    data["event_types"][0]["fields"] = [{"key": "x", "label": "X", "type": "string"}]
    errors = _errors_for(data)
    assert any("sections" in e and "fields" in e for e in errors)


def test_sections_validation():
    import copy

    data = copy.deepcopy(SECTIONED)
    data["event_types"][0]["sections"] = []
    errors = _errors_for(data)
    assert "event_types[0].sections: at least one section is required" in errors

    data = copy.deepcopy(SECTIONED)
    data["event_types"][0]["sections"][0]["fields"] = []
    errors = _errors_for(data)
    assert "event_types[0].sections[0].fields: at least one field is required" in errors

    data = copy.deepcopy(SECTIONED)
    data["event_types"][0]["sections"][1]["fields"][0]["key"] = "at"  # dup across sections
    errors = _errors_for(data)
    assert any("duplicate key 'at'" in e for e in errors)

    data = copy.deepcopy(SECTIONED)
    data["event_types"][0]["sections"][0]["fields"][0]["column"] = "right"  # 1-col section
    errors = _errors_for(data)
    assert any("'right' requires" in e and "columns: 2" in e for e in errors)


def test_explicit_empty_fields_and_is_collection():
    spec = parse_spec(
        {
            "category": {"value": "c1", "display": "C1"},
            "event_types": [
                {
                    "value": "incident_collection",
                    "display": "Incident",
                    "is_collection": True,
                    "fields": [],
                }
            ],
        }
    )
    et = spec.event_types[0]
    assert et.is_collection is True
    assert et.fields == []

    # a MISSING fields key (with no sections) is still an error
    errors = _errors_for(
        {
            "category": {"value": "c1", "display": "C1"},
            "event_types": [{"value": "t1", "display": "T1"}],
        }
    )
    assert "event_types[0].fields: at least one field is required" in errors


TOPLEVEL_CHOICES = {
    "category": {"value": "c1", "display": "C1"},
    "choices": {
        "shared_actions": [
            {"value": "stopped", "display": "Stopped", "icon": "stop_icon"},
            "warned",
        ]
    },
    "event_types": [
        {
            "value": "t1",
            "display": "T1",
            "fields": [
                {
                    "key": "action",
                    "label": "Action",
                    "type": "select",
                    "choices_field": "shared_actions",
                },
            ],
        },
        {
            "value": "t2",
            "display": "T2",
            "fields": [
                {
                    "key": "response",
                    "label": "Response",
                    "type": "multiselect",
                    "choices_field": "shared_actions",
                },
            ],
        },
    ],
}


def test_toplevel_choices_parse():
    import copy

    spec = parse_spec(copy.deepcopy(TOPLEVEL_CHOICES))
    opts = spec.choices["shared_actions"]
    assert [(o.value, o.display, o.icon) for o in opts] == [
        ("stopped", "Stopped", "stop_icon"),
        ("warned", "Warned", None),
    ]
    # both fields reference the set; no inline options
    assert spec.event_types[0].fields[0].options is None
    assert spec.event_types[1].fields[0].options is None


def test_choices_field_reference_validation():
    import copy

    data = copy.deepcopy(TOPLEVEL_CHOICES)
    data["event_types"][0]["fields"][0]["choices_field"] = "nope"
    errors = _errors_for(data)
    assert any("references an undeclared top-level choice set" in e for e in errors)

    data = copy.deepcopy(TOPLEVEL_CHOICES)
    data["event_types"][0]["fields"][0]["options"] = ["stopped"]
    errors = _errors_for(data)
    assert any("declared top-level" in e and "inline" in e for e in errors)

    data = copy.deepcopy(TOPLEVEL_CHOICES)
    data["choices"] = {"Bad Name!": ["a"]}
    errors = _errors_for(data)
    assert any("choices" in e and "must match" in e for e in errors)


def test_option_icon_validation():
    errors = _errors_for(
        _spec_with_field(
            {
                "key": "s",
                "label": "S",
                "type": "select",
                "options": [{"value": "a", "display": "A", "icon": 3}],
            }
        )
    )
    assert any("icon: must be a string" in e for e in errors)


def test_event_type_defaults_and_readonly_parse():
    data = _spec_with_field({"key": "n", "label": "N", "type": "string"})
    et = data["event_types"][0]
    et["default_priority"] = "amber"
    et["default_state"] = "active"
    et["readonly"] = True
    spec = parse_spec(data)
    parsed = spec.event_types[0]
    assert parsed.default_priority == 200
    assert parsed.default_state == "active"
    assert parsed.readonly is True

    data["event_types"][0]["default_priority"] = 300
    assert parse_spec(data).event_types[0].default_priority == 300


def test_event_type_defaults_validation():
    data = _spec_with_field({"key": "n", "label": "N", "type": "string"})
    data["event_types"][0]["default_priority"] = "purple"
    errors = _errors_for(data)
    assert any("default_priority" in e and "gray, green, amber, red" in e for e in errors)
    data = _spec_with_field({"key": "n", "label": "N", "type": "string"})
    data["event_types"][0]["default_priority"] = 150
    errors = _errors_for(data)
    assert any("default_priority" in e for e in errors)
    data = _spec_with_field({"key": "n", "label": "N", "type": "string"})
    data["event_types"][0]["default_state"] = "closed"
    errors = _errors_for(data)
    assert any("default_state" in e and "new, active, resolved" in e for e in errors)
    data = _spec_with_field({"key": "n", "label": "N", "type": "string"})
    data["event_types"][0]["readonly"] = "yes"
    errors = _errors_for(data)
    assert any("readonly: must be true or false" in e for e in errors)


def test_default_priority_rejects_non_integer_values():
    # unhashable values must produce SpecError, not TypeError (Copilot r3908440534)
    data = _spec_with_field({"key": "n", "label": "N", "type": "string"})
    data["event_types"][0]["default_priority"] = []
    errors = _errors_for(data)
    assert any("default_priority" in e for e in errors)
    data["event_types"][0]["default_priority"] = 100.0
    errors = _errors_for(data)
    assert any("default_priority" in e for e in errors)


def test_geometry_type_parses_and_validates():
    data = _spec_with_field({"key": "n", "label": "N", "type": "string"})
    data["event_types"][0]["geometry_type"] = "polygon"
    assert parse_spec(data).event_types[0].geometry_type == "Polygon"
    data["event_types"][0]["geometry_type"] = "Point"
    assert parse_spec(data).event_types[0].geometry_type == "Point"
    data["event_types"][0]["geometry_type"] = "line"
    errors = _errors_for(data)
    assert any("geometry_type" in e and "point, polygon" in e for e in errors)


def test_auto_resolve_resolve_time_and_ordernum_parse():
    data = _spec_with_field({"key": "n", "label": "N", "type": "string"})
    et = data["event_types"][0]
    et["auto_resolve"] = True
    et["resolve_time"] = 12
    et["ordernum"] = 30
    parsed = parse_spec(data).event_types[0]
    assert parsed.auto_resolve is True
    assert parsed.resolve_time == 12
    assert parsed.ordernum == 30


def test_auto_resolve_validation():
    data = _spec_with_field({"key": "n", "label": "N", "type": "string"})
    data["event_types"][0]["auto_resolve"] = True  # inert without resolve_time
    errors = _errors_for(data)
    assert any("auto_resolve" in e and "resolve_time" in e for e in errors)

    data = _spec_with_field({"key": "n", "label": "N", "type": "string"})
    data["event_types"][0]["resolve_time"] = -1
    errors = _errors_for(data)
    assert any("resolve_time" in e and "positive" in e for e in errors)

    data = _spec_with_field({"key": "n", "label": "N", "type": "string"})
    data["event_types"][0]["resolve_time"] = True
    errors = _errors_for(data)
    assert any("resolve_time" in e for e in errors)

    data = _spec_with_field({"key": "n", "label": "N", "type": "string"})
    data["event_types"][0]["ordernum"] = "first"
    errors = _errors_for(data)
    assert any("ordernum" in e and "number" in e for e in errors)


CONDITIONAL = {
    "category": {"value": "c1", "display": "C1"},
    "event_types": [
        {
            "value": "fire",
            "display": "Fire",
            "sections": [
                {
                    "label": "",
                    "fields": [
                        {
                            "key": "cause",
                            "label": "Cause",
                            "type": "select",
                            "options": ["manmade", "natural"],
                        }
                    ],
                },
                {
                    "label": "Arson",
                    "condition": {"field": "cause", "operator": "is_exactly", "value": "manmade"},
                    "fields": [{"key": "investigator", "label": "Investigator", "type": "string"}],
                },
                {
                    "label": "Notes",
                    "active": False,
                    "fields": [{"key": "notes", "label": "Notes", "type": "textarea"}],
                },
            ],
            "required": ["cause", "investigator"],
        }
    ],
}


def test_section_condition_and_active_parse():
    import copy

    spec = parse_spec(copy.deepcopy(CONDITIONAL))
    sections = spec.event_types[0].sections
    assert sections[0].condition is None and sections[0].active is True
    cond = sections[1].condition
    assert (cond.field, cond.operator, cond.value) == ("cause", "is_exactly", "manmade")
    assert sections[2].active is False


def test_section_condition_validation():
    import copy

    data = copy.deepcopy(CONDITIONAL)
    data["event_types"][0]["sections"][1]["condition"]["operator"] = "contains"
    errors = _errors_for(data)
    assert any("operator" in e and "is_exactly" in e for e in errors)

    data = copy.deepcopy(CONDITIONAL)
    data["event_types"][0]["sections"][1]["condition"]["field"] = "nope"
    errors = _errors_for(data)
    assert any("condition.field" in e and "nope" in e for e in errors)

    data = copy.deepcopy(CONDITIONAL)
    data["event_types"][0]["sections"][1]["condition"]["field"] = "investigator"
    errors = _errors_for(data)
    assert any("own section" in e for e in errors)

    data = copy.deepcopy(CONDITIONAL)
    data["event_types"][0]["sections"][2]["active"] = "no"
    errors = _errors_for(data)
    assert any("active: must be true or false" in e for e in errors)


def test_ordernum_accepts_fractional_ranks():
    # ER's event-type ranking produces fractional ordernums (e.g. 0.5)
    data = _spec_with_field({"key": "n", "label": "N", "type": "string"})
    data["event_types"][0]["ordernum"] = 0.5
    assert parse_spec(data).event_types[0].ordernum == 0.5


def test_field_active_false_parses():
    spec = parse_spec(
        _spec_with_field({"key": "old", "label": "Old", "type": "string", "active": False})
    )
    assert spec.event_types[0].fields[0].active is False
    errors = _errors_for(
        _spec_with_field({"key": "old", "label": "Old", "type": "string", "active": "no"})
    )
    assert any("active: must be true or false" in e for e in errors)


COLLECTION_FIELD = {
    "key": "sightings",
    "label": "Sightings",
    "type": "collection",
    "item_name": "sighting",
    "button_text": "Add sighting",
    "fields": [
        {"key": "text_1", "label": "Text 1", "type": "string"},
        {"key": "count", "label": "Count", "type": "integer"},
    ],
    "required": ["text_1"],
}


def test_collection_field_parses():
    import copy

    spec = parse_spec(_spec_with_field(copy.deepcopy(COLLECTION_FIELD)))
    f = spec.event_types[0].fields[0]
    assert f.type == "collection"
    assert f.item_name == "sighting"
    assert f.button_text == "Add sighting"
    assert [sub.key for sub in f.fields] == ["text_1", "count"]
    assert f.required == ["text_1"]


def test_collection_validation():
    import copy

    data = copy.deepcopy(COLLECTION_FIELD)
    del data["item_name"]
    errors = _errors_for(_spec_with_field(data))
    assert any("item_name" in e and "required" in e for e in errors)

    data = copy.deepcopy(COLLECTION_FIELD)
    data["fields"] = []
    errors = _errors_for(_spec_with_field(data))
    assert any("fields" in e and "at least one" in e for e in errors)

    data = copy.deepcopy(COLLECTION_FIELD)
    data["fields"].append({"key": "species", "label": "S", "type": "select", "options": ["a"]})
    errors = _errors_for(_spec_with_field(data))
    assert any("'select' is not supported inside a collection" in e for e in errors)

    data = copy.deepcopy(COLLECTION_FIELD)
    data["required"] = ["nope"]
    errors = _errors_for(_spec_with_field(data))
    assert any("'nope' is not a declared" in e for e in errors)

    data = copy.deepcopy(COLLECTION_FIELD)
    data["fields"][1]["key"] = "text_1"
    errors = _errors_for(_spec_with_field(data))
    assert any("duplicate key 'text_1'" in e for e in errors)
