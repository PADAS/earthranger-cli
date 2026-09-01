import copy

import pytest
import yaml

from conftest import FakeER
from earthranger_cli.apply import apply_spec
from earthranger_cli.dsl import parse_spec
from earthranger_cli.pull import PullError, pull_category, render_spec_yaml
from earthranger_cli.schema_gen import build_event_type_payload

SPEC_DATA = {
    "category": {"value": "wm", "display": "Wildlife Monitoring"},
    "event_types": [
        {
            "value": "sighting",
            "display": "Sighting",
            "icon_id": "mammal_rep",
            "fields": [
                {
                    "key": "species",
                    "label": "Species",
                    "type": "select",
                    "hint": "pick one",
                    "description": "The species",
                    "options": [
                        {"value": "elephant", "display": "Elephant"},
                        {"value": "lion_cub", "display": "Young Lion"},
                    ],
                },
                {"key": "threats", "label": "Threats", "type": "multiselect", "options": ["a"]},
                {"key": "count", "label": "Count", "type": "number", "min": 0, "default": 1},
                {"key": "ok", "label": "OK", "type": "boolean", "default": False},
                {"key": "seen", "label": "Seen", "type": "date"},
                {"key": "at", "label": "At", "type": "datetime"},
                {"key": "link", "label": "Link", "type": "url"},
                {"key": "mail", "label": "Mail", "type": "string", "format": "email"},
                {"key": "notes", "label": "Notes", "type": "textarea"},
            ],
            "required": ["species"],
        },
        {
            "value": "inactive_type",
            "display": "Old",
            "is_active": False,
            "fields": [{"key": "notes", "label": "Notes", "type": "string"}],
        },
    ],
}


def _server_from_spec(spec_data):
    """Build FakeER state as ER's GET would return it for an applied spec."""
    from earthranger_cli.choices import desired_choice_sets

    spec = parse_spec(copy.deepcopy(spec_data))
    event_types = []
    for et in spec.event_types:
        payload = build_event_type_payload(et, spec.category.value)
        record = copy.deepcopy(payload)
        record["id"] = f"id-{et.value}"
        record["category"] = {"value": spec.category.value}
        record["icon"] = record.pop("icon", None)
        event_types.append(record)
    choices = {
        name: [{**rec, "id": f"ch-{name}-{rec['value']}"} for rec in recs]
        for name, recs in desired_choice_sets(spec).items()
    }
    return FakeER(
        categories=[
            {"id": "cat-1", "value": spec.category.value, "display": spec.category.display}
        ],
        event_types=event_types,
        choices=choices,
    )


def test_pull_round_trips_to_all_unchanged():
    fake = _server_from_spec(SPEC_DATA)
    result = pull_category(fake, "wm")
    assert result.unsupported == []
    pulled = parse_spec(result.spec)
    records = apply_spec(fake, pulled)
    assert {r.action for r in records} == {"unchanged"}
    assert fake.writes() == []


def test_pull_yaml_parses_and_uses_option_shorthand():
    fake = _server_from_spec(SPEC_DATA)
    result = pull_category(fake, "wm")
    text = render_spec_yaml(result)
    assert parse_spec(yaml.safe_load(text)).category.value == "wm"
    # display "Elephant" is derivable from value "elephant" -> bare string
    assert "- elephant" in text
    # display "Young Lion" is NOT derivable from "lion_cub" -> mapping
    assert "value: lion_cub" in text


def test_pull_missing_category_raises():
    fake = FakeER(categories=[])
    with pytest.raises(PullError, match="no category with value 'nope'"):
        pull_category(fake, "nope")


def test_pull_reports_unsupported_field_shapes():
    fake = _server_from_spec(SPEC_DATA)
    et = fake.event_types[0]
    et["schema"]["json"]["properties"]["loc"] = {
        "type": "object",
        "title": "Where",
        "deprecated": False,
    }
    et["schema"]["ui"]["fields"]["loc"] = {"type": "LOCATION", "parent": "section-1"}
    et["schema"]["ui"]["sections"]["section-1"]["leftColumn"].append(
        {"name": "loc", "type": "field"}
    )
    result = pull_category(fake, "wm")
    assert any("loc" in w and "sighting" in w for w in result.unsupported)
    # the rest of the event type still pulls
    values = [t["value"] for t in result.spec["event_types"]]
    assert "sighting" in values


def test_pull_foreign_choice_field_name_becomes_choices_field():
    fake = _server_from_spec(SPEC_DATA)
    et = fake.event_types[0]
    prop = et["schema"]["json"]["properties"]["species"]
    prop["anyOf"] = [{"$ref": "/api/v2.0/schemas/choices.json?field=handmade_name"}]
    fake.choices["handmade_name"] = [
        {"id": "h1", "value": "x", "display": "X", "is_active": True, "ordernum": 0}
    ]
    result = pull_category(fake, "wm")
    assert result.unsupported == []
    field = result.spec["event_types"][0]["fields"][0]
    assert field["choices_field"] == "handmade_name"
    assert field["options"] == ["x"]
    records = apply_spec(fake, parse_spec(result.spec))
    assert {r.action for r in records} == {"unchanged"}


def test_pull_unusable_choice_field_name_still_skipped():
    fake = _server_from_spec(SPEC_DATA)
    et = fake.event_types[0]
    prop = et["schema"]["json"]["properties"]["species"]
    prop["anyOf"] = [{"$ref": "/api/v2.0/schemas/choices.json?field=bad.na%20me"}]
    result = pull_category(fake, "wm")
    assert any("bad.na%20me" in w for w in result.unsupported)


def test_pull_reports_layout_it_cannot_express():
    # an empty section has no DSL form (sections require fields)
    fake = _server_from_spec(SPEC_DATA)
    et = fake.event_types[0]
    et["schema"]["ui"]["sections"]["section-2"] = {
        "label": "More",
        "columns": 1,
        "isActive": True,
        "leftColumn": [],
        "rightColumn": [],
    }
    et["schema"]["ui"]["order"] = ["section-1", "section-2"]
    result = pull_category(fake, "wm")
    assert any("sighting" in w and "layout" in w and "no fields" in w for w in result.unsupported)
    # the fieldless section is dropped (a warning); the rest still pulls
    pulled = next(t for t in result.spec["event_types"] if t["value"] == "sighting")
    assert "sections" not in pulled  # collapsed back to the single-section form


def test_pull_skips_schema_with_auto_generate():
    fake = _server_from_spec(SPEC_DATA)
    et = fake.event_types[0]
    et["schema"]["auto-generate"] = True
    result = pull_category(fake, "wm")
    assert any(
        w == "event type 'sighting': schema uses auto-generate; skipped entirely"
        for w in result.unsupported
    )
    assert "sighting" not in [t["value"] for t in result.spec["event_types"]]


def test_pull_skips_event_type_with_inactive_section():
    fake = _server_from_spec(SPEC_DATA)
    et = fake.event_types[0]
    et["schema"]["ui"]["sections"]["section-1"]["isActive"] = False
    result = pull_category(fake, "wm")
    assert any(
        "sighting" in w and "layout section 'section-1' is inactive (isActive: false)" in w
        for w in result.unsupported
    )
    assert "sighting" not in [t["value"] for t in result.spec["event_types"]]


def test_pull_handles_json_stringified_v2_schema():
    import json as _json

    fake = _server_from_spec(SPEC_DATA)
    et = fake.event_types[0]
    et["schema"] = _json.dumps(et["schema"])  # ER sometimes stringifies on GET
    result = pull_category(fake, "wm")
    assert result.unsupported == []
    assert "sighting" in [t["value"] for t in result.spec["event_types"]]


def test_pull_skips_v1_event_types():
    fake = _server_from_spec(SPEC_DATA)
    fake.event_types.append(
        {
            "id": "id-old",
            "value": "legacy_v1",
            "display": "Legacy",
            "category": {"value": "wm"},
            "is_active": True,
            "schema": '{\n "schema": {"$schema": "http://json-schema.org/draft-04/schema#"},\n "definition": []\n}',
        }
    )
    result = pull_category(fake, "wm")
    assert any("legacy_v1" in w and "v2 json/ui envelope" in w for w in result.unsupported)
    assert "legacy_v1" not in [t["value"] for t in result.spec["event_types"]]


TWO_COL_SPEC = {
    "category": {"value": "wm", "display": "Wildlife Monitoring"},
    "event_types": [
        {
            "value": "traffic",
            "display": "Traffic",
            "layout": {"label": "", "columns": 2},
            "fields": [
                {"key": "a", "label": "A", "type": "string"},
                {"key": "b", "label": "B", "type": "number", "column": "right"},
                {"key": "c", "label": "C", "type": "string"},
            ],
        }
    ],
}


def test_pull_round_trips_two_column_layout():
    fake = _server_from_spec(TWO_COL_SPEC)
    result = pull_category(fake, "wm")
    assert result.unsupported == []
    et = result.spec["event_types"][0]
    assert et["layout"] == {"label": "", "columns": 2}
    # pull canonicalizes field order to left column then right column;
    # the wire schema is identical either way (verified by the apply below)
    assert [(f["key"], f.get("column")) for f in et["fields"]] == [
        ("a", None),
        ("c", None),
        ("b", "right"),
    ]
    records = apply_spec(fake, parse_spec(result.spec))
    assert {r.action for r in records} == {"unchanged"}


def test_pull_default_layout_omits_layout_key():
    fake = _server_from_spec(SPEC_DATA)
    result = pull_category(fake, "wm")
    assert all("layout" not in et for et in result.spec["event_types"])


def test_pull_section_not_in_order_is_unsupported():
    # a section missing from ui.order is a shape the generator never makes
    fake = _server_from_spec(SPEC_DATA)
    fake.event_types[0]["schema"]["ui"]["sections"]["section-2"] = {
        "label": "More",
        "columns": 1,
        "isActive": True,
        "leftColumn": [],
        "rightColumn": [],
    }
    result = pull_category(fake, "wm")
    assert any("layout" in w and "order" in w for w in result.unsupported)


def test_pull_foreign_section_id_still_unsupported():
    fake = _server_from_spec(SPEC_DATA)
    et = fake.event_types[0]
    ui = et["schema"]["ui"]
    ui["sections"] = {"section-custom": ui["sections"].pop("section-1")}
    ui["order"] = ["section-custom"]
    result = pull_category(fake, "wm")
    assert any("section-custom" in w and "layout" in w for w in result.unsupported)


MULTI_SECTION_SPEC = {
    "category": {"value": "wm", "display": "Wildlife Monitoring"},
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
            "required": ["at"],
        }
    ],
}


def test_pull_round_trips_multi_section():
    fake = _server_from_spec(MULTI_SECTION_SPEC)
    result = pull_category(fake, "wm")
    assert result.unsupported == []
    et = result.spec["event_types"][0]
    assert "fields" not in et and "layout" not in et
    assert [s.get("columns") for s in et["sections"]] == [None, 2]  # 1 omitted as default
    assert [s.get("label") for s in et["sections"]] == ["", ""]
    records = apply_spec(fake, parse_spec(result.spec))
    assert {r.action for r in records} == {"unchanged"}
    assert fake.writes() == []


def test_pull_single_default_section_still_emits_flat_form():
    fake = _server_from_spec(SPEC_DATA)
    result = pull_category(fake, "wm")
    for et in result.spec["event_types"]:
        assert "sections" not in et
        assert "fields" in et


def test_pull_all_inactive_choices_round_trip_to_empty_options():
    fake = _server_from_spec(SPEC_DATA)
    for rec in fake.choices["sighting_species"]:
        rec["is_active"] = False
    result = pull_category(fake, "wm")
    assert result.unsupported == []
    et = next(t for t in result.spec["event_types"] if t["value"] == "sighting")
    species = next(f for f in et["fields"] if f["key"] == "species")
    assert species["options"] == []
    records = apply_spec(fake, parse_spec(result.spec))
    assert {r.action for r in records} == {"unchanged"}


def test_pull_fieldless_collection_round_trips():
    fake = _server_from_spec(SPEC_DATA)
    fake.event_types.append(
        {
            "id": "id-ic",
            "value": "incident_collection",
            "display": "Incident",
            "category": {"value": "wm"},
            "is_active": True,
            "is_collection": True,
            "icon": None,
            "schema": {
                "json": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "unevaluatedProperties": False,
                    "properties": {},
                    "required": [],
                },
                "ui": {"fields": {}, "headers": {}, "order": [], "sections": {}},
            },
        }
    )
    result = pull_category(fake, "wm")
    assert result.unsupported == []
    pulled_et = next(t for t in result.spec["event_types"] if t["value"] == "incident_collection")
    assert pulled_et["is_collection"] is True
    assert pulled_et["fields"] == []
    records = apply_spec(fake, parse_spec(result.spec))
    assert {r.action for r in records} == {"unchanged"}


SHARED_SET_SPEC = {
    "category": {"value": "wm", "display": "Wildlife Monitoring"},
    "choices": {
        "shared_actions": [
            {"value": "stopped", "display": "Halted"},
            {"value": "warned", "display": "Warned", "icon": "warn_icon"},
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
                }
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
                }
            ],
        },
    ],
}


def test_pull_hoists_shared_sets_and_round_trips():
    fake = _server_from_spec(SHARED_SET_SPEC)
    result = pull_category(fake, "wm")
    assert result.unsupported == []
    spec = result.spec
    assert list(spec["choices"]) == ["shared_actions"]
    assert spec["choices"]["shared_actions"] == [
        {"value": "stopped", "display": "Halted"},
        {"value": "warned", "display": "Warned", "icon": "warn_icon"},
    ]
    for et in spec["event_types"]:
        f = et["fields"][0]
        assert f["choices_field"] == "shared_actions"
        assert "options" not in f
    records = apply_spec(fake, parse_spec(spec))
    assert {r.action for r in records} == {"unchanged"}
    assert fake.writes() == []


def test_pull_orders_options_by_ordernum():
    fake = _server_from_spec(SPEC_DATA)
    recs = fake.choices["sighting_species"]
    for r, num in zip(recs, [1, 0]):
        r["ordernum"] = num
    result = pull_category(fake, "wm")
    field = result.spec["event_types"][0]["fields"][0]
    values = [o["value"] if isinstance(o, dict) else o for o in field["options"]]
    assert values == ["lion_cub", "elephant"]
    records = apply_spec(fake, parse_spec(result.spec))
    assert {r.action for r in records} == {"unchanged"}


def test_pull_emits_nondefault_state_priority_readonly():
    fake = _server_from_spec(SPEC_DATA)
    et = fake.event_types[0]
    et["default_priority"] = 200
    et["default_state"] = "active"
    et["readonly"] = True
    fake.event_types[1]["default_priority"] = 0  # defaults stay omitted
    result = pull_category(fake, "wm")
    pulled = result.spec["event_types"][0]
    assert pulled["default_priority"] == "amber"
    assert pulled["default_state"] == "active"
    assert pulled["readonly"] is True
    other = result.spec["event_types"][1]
    for key in ("default_priority", "default_state", "readonly"):
        assert key not in other
    records = apply_spec(fake, parse_spec(result.spec))
    assert {r.action for r in records} == {"unchanged"}


def test_pull_emits_nondefault_geometry_type():
    fake = _server_from_spec(SPEC_DATA)
    fake.event_types[0]["geometry_type"] = "Polygon"
    fake.event_types[1]["geometry_type"] = "Point"
    result = pull_category(fake, "wm")
    assert result.spec["event_types"][0]["geometry_type"] == "polygon"
    assert "geometry_type" not in result.spec["event_types"][1]
    records = apply_spec(fake, parse_spec(result.spec))
    assert {r.action for r in records} == {"unchanged"}


def test_pull_emits_auto_resolve_and_ordernum():
    fake = _server_from_spec(SPEC_DATA)
    fake.event_types[0]["auto_resolve"] = True
    fake.event_types[0]["resolve_time"] = 12.0
    fake.event_types[0]["ordernum"] = 30.0
    result = pull_category(fake, "wm")
    pulled = result.spec["event_types"][0]
    assert pulled["auto_resolve"] is True
    assert pulled["resolve_time"] == 12
    assert pulled["ordernum"] == 30
    other = result.spec["event_types"][1]
    for key in ("auto_resolve", "resolve_time", "ordernum"):
        assert key not in other
    records = apply_spec(fake, parse_spec(result.spec))
    assert {r.action for r in records} == {"unchanged"}
