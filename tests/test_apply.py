import copy

import pytest

from conftest import FakeER
from er_events_cli.apply import apply_spec, extract_choice_fields, normalize_v2_schema
from er_events_cli.dsl import parse_spec
from er_events_cli.schema_gen import build_event_type_payload

SPEC_DATA = {
    "category": {"value": "wm", "display": "Wildlife Monitoring"},
    "event_types": [
        {
            "value": "sighting",
            "display": "Sighting",
            "fields": [
                {
                    "key": "species",
                    "label": "Species",
                    "type": "select",
                    "options": [{"value": "elephant", "display": "Elephant"}],
                },
                {"key": "notes", "label": "Notes", "type": "string"},
            ],
        }
    ],
}


def _spec():
    return parse_spec(copy.deepcopy(SPEC_DATA))


def _existing_state():
    """Server state exactly matching the spec (as ER's GET would return it)."""
    spec = _spec()
    payload = build_event_type_payload(spec.event_types[0], "wm")
    existing_type = copy.deepcopy(payload)
    existing_type["id"] = "et-1"
    # simulate ER's GET quirk: additionalProperties instead of unevaluatedProperties
    del existing_type["schema"]["json"]["unevaluatedProperties"]
    existing_type["schema"]["json"]["additionalProperties"] = False
    # simulate ER returning the category as a nested dict
    existing_type["category"] = {"value": "wm", "display": "Wildlife Monitoring"}
    return FakeER(
        categories=[{"id": "cat-1", "value": "wm", "display": "Wildlife Monitoring"}],
        event_types=[existing_type],
        choices={
            "sighting_species": [
                {
                    "id": "ch-1",
                    "model": "activity.event",
                    "field": "sighting_species",
                    "value": "elephant",
                    "display": "Elephant",
                    "is_active": True,
                },
            ]
        },
    )


def test_normalize_v2_schema_repairs_get_response():
    schema = {"json": {"additionalProperties": False, "properties": {}}}
    fixed = normalize_v2_schema(schema)
    assert fixed["json"]["unevaluatedProperties"] is False
    assert "additionalProperties" not in fixed["json"]


def test_extract_choice_fields():
    schema = {
        "json": {
            "properties": {
                "a": {"anyOf": [{"$ref": "/api/v2.0/schemas/choices.json?field=t_a"}]},
                "b": {"items": {"anyOf": [{"$ref": "/api/v2.0/schemas/choices.json?field=t_b"}]}},
                "c": {"type": "string"},
            }
        }
    }
    assert extract_choice_fields(schema) == ["t_a", "t_b"]


def test_fresh_site_creates_everything():
    fake = FakeER()
    records = apply_spec(fake, _spec())
    by = {(r.kind, r.name): r.action for r in records}
    assert by[("category", "wm")] == "created"
    assert by[("choice", "sighting_species:elephant")] == "created"
    assert by[("event_type", "sighting")] == "created"
    methods = [c[0] for c in fake.writes()]
    assert "post_event_category" in methods
    assert "_post" in methods
    assert "post_event_type" in methods
    posted_type = next(c for c in fake.calls if c[0] == "post_event_type")
    assert posted_type[2] == "v2.0"


def test_matching_site_is_all_unchanged():
    fake = _existing_state()
    records = apply_spec(fake, _spec())
    assert {r.action for r in records} == {"unchanged"}
    assert fake.writes() == []


def test_display_change_patches_event_type_with_id():
    fake = _existing_state()
    spec = _spec()
    spec.event_types[0].display = "Animal Sighting"
    records = apply_spec(fake, spec)
    assert ("event_type", "sighting") in {
        (r.kind, r.name) for r in records if r.action == "updated"
    }
    patched = next(c for c in fake.calls if c[0] == "patch_event_type")
    assert patched[1]["id"] == "et-1"
    assert patched[1]["display"] == "Animal Sighting"
    assert patched[2] == "v2.0"


def test_category_display_change_patches_category():
    fake = _existing_state()
    spec = _spec()
    spec.category.display = "Wildlife"
    apply_spec(fake, spec)
    patched = next(c for c in fake.calls if c[0] == "patch_event_category")
    assert patched[1] == {"id": "cat-1", "value": "wm", "display": "Wildlife"}


def test_removed_option_is_deactivated():
    fake = _existing_state()
    fake.choices["sighting_species"].append(
        {
            "id": "ch-2",
            "model": "activity.event",
            "field": "sighting_species",
            "value": "rhino",
            "display": "Rhino",
            "is_active": True,
        }
    )
    records = apply_spec(fake, _spec())
    assert any(r.action == "deactivated" and r.name == "sighting_species:rhino" for r in records)
    patched = next(c for c in fake.calls if c[0] == "_patch")
    assert patched[1] == "choices/ch-2"
    assert patched[2] == {"is_active": False}


def test_dry_run_performs_no_writes():
    fake = FakeER()
    records = apply_spec(fake, _spec(), dry_run=True)
    assert fake.writes() == []
    assert any(r.action == "created" for r in records)


def test_api_write_failure_names_the_failing_object():
    from erclient.er_errors import ERClientException

    from er_events_cli.apply import ApplyError

    fake = FakeER()

    def boom(event_type, version="v1.0"):
        raise ERClientException("400 schema invalid")

    fake.post_event_type = boom
    with pytest.raises(ApplyError) as exc:
        apply_spec(fake, _spec())
    assert "creating event_type 'sighting': 400 schema invalid" in str(exc.value)


def test_icon_change_patches_event_type():
    fake = _existing_state()
    spec = _spec()
    spec.event_types[0].icon_id = "cameratrap_rep"
    records = apply_spec(fake, spec)
    assert any(r.kind == "event_type" and r.action == "updated" for r in records)
    patched = next(c for c in fake.calls if c[0] == "patch_event_type")
    assert patched[1]["icon"] == "cameratrap_rep"


def test_stringified_schema_from_get_still_compares_unchanged():
    import json as _json

    fake = _existing_state()
    et = fake.event_types[0]
    et["schema"] = _json.dumps(et["schema"])  # ER sometimes stringifies on GET
    records = apply_spec(fake, _spec())
    assert {r.action for r in records} == {"unchanged"}
    assert fake.writes() == []


def test_server_echo_noise_still_compares_unchanged():
    """ER's GET injects icon_id/image_url into the schema dict and stores
    empty-string description/placeholder where the generator omits the key;
    none of that is a real difference."""
    fake = _existing_state()
    schema = fake.event_types[0]["schema"]
    schema["icon_id"] = "sighting"
    schema["image_url"] = "https://x.pamdas.org/static/sighting-black.svg"
    schema["json"]["properties"]["notes"]["description"] = ""
    schema["ui"]["fields"]["notes"]["placeholder"] = ""
    schema["ui"]["fields"]["notes"]["conditionalDependents"] = []
    schema["ui"]["sections"]["section-1"]["conditions"] = []
    records = apply_spec(fake, _spec())
    assert {r.action for r in records} == {"unchanged"}
    assert fake.writes() == []
