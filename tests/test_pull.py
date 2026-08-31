import copy

import pytest
import yaml

from conftest import FakeER
from er_events_cli.apply import apply_spec
from er_events_cli.dsl import parse_spec
from er_events_cli.pull import PullError, pull_category, render_spec_yaml
from er_events_cli.schema_gen import build_event_type_payload

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
    spec = parse_spec(copy.deepcopy(spec_data))
    event_types = []
    choices = {}
    for et in spec.event_types:
        payload = build_event_type_payload(et, spec.category.value)
        record = copy.deepcopy(payload)
        record["id"] = f"id-{et.value}"
        record["category"] = {"value": spec.category.value}
        record["icon"] = record.pop("icon", None)
        event_types.append(record)
        for f in et.fields:
            if f.options is None:
                continue
            name = f"{et.value}_{f.key}"
            choices[name] = [
                {
                    "id": f"ch-{name}-{o.value}",
                    "field": name,
                    "value": o.value,
                    "display": o.display,
                    "is_active": True,
                }
                for o in f.options
            ]
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


def test_pull_reports_foreign_choice_field_name():
    fake = _server_from_spec(SPEC_DATA)
    et = fake.event_types[0]
    prop = et["schema"]["json"]["properties"]["species"]
    prop["anyOf"] = [{"$ref": "/api/v2.0/schemas/choices.json?field=handmade_name"}]
    result = pull_category(fake, "wm")
    assert any("handmade_name" in w for w in result.unsupported)


def test_pull_reports_layout_it_cannot_express():
    fake = _server_from_spec(SPEC_DATA)
    et = fake.event_types[0]
    et["schema"]["ui"]["sections"]["section-2"] = {
        "label": "More",
        "columns": 1,
        "isActive": True,
        "leftColumn": [],
        "rightColumn": [],
    }
    result = pull_category(fake, "wm")
    assert any("sighting" in w and "layout" in w for w in result.unsupported)
    values = [t["value"] for t in result.spec["event_types"]]
    assert "sighting" not in values  # whole event type skipped
    assert "inactive_type" in values


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
