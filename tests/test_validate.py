"""validate.py: check event_details against the type's server-rendered schema."""

import pytest
from erclient.er_errors import ERClientNotFound

from conftest import FakeER
from earthranger_cli.validate import UnknownEventType, validate_events

V2_TYPE = {"value": "animal_sighting", "version": "2", "display": "Animal Sighting"}
# What GET /api/v2.0/activity/eventtypes/animal_sighting/schema?pre_render=true&s_format=enum
# returns: choices dereferenced into enums, strictness properties stripped by das.
V2_RENDERED = {
    "json": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "species": {"type": "string", "title": "Species", "enum": ["elephant", "lion"]},
            "count": {"type": "number", "title": "Number of animals", "minimum": 0},
            "notes": {"type": "string", "title": "Notes"},
        },
        "required": ["species"],
    },
    "ui": {},
}

V1_TYPE = {"value": "fence_break", "version": "1", "display": "Fence Break"}
# What GET /api/v1.0/activity/events/schema/eventtype/fence_break returns: the
# jinja-rendered v1 envelope with inline enums.
V1_RENDERED = {
    "schema": {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "type": "object",
        "properties": {
            "fence_name": {"type": "string", "title": "Fence", "enum": ["north", "south"]},
            "repaired": {"type": "boolean", "title": "Repaired"},
        },
        "required": ["fence_name"],
    },
    "definition": ["fence_name", "repaired"],
}


def _fake():
    fake = FakeER()
    fake.responses["activity/eventtypes/animal_sighting"] = V2_TYPE
    fake.responses["activity/eventtypes/animal_sighting/schema"] = V2_RENDERED
    fake.responses["activity/eventtypes/fence_break"] = V1_TYPE
    fake.responses["activity/events/schema/eventtype/fence_break"] = V1_RENDERED
    return fake


def _ev(event_type, **details):
    return {"event_type": event_type, "event_details": details}


def test_valid_events_produce_no_errors():
    fake = _fake()
    errors = validate_events(
        fake,
        [_ev("animal_sighting", species="lion", count=3), _ev("fence_break", fence_name="north")],
    )
    assert errors == [[], []]


def test_v2_violations_are_named_per_field():
    errors = validate_events(
        _fake(), [_ev("animal_sighting", species="zebra", count=-1, colour="grey")]
    )
    assert errors == [
        [
            "colour: not a field of 'animal_sighting'",
            "count: -1 is less than the minimum of 0",
            "species: 'zebra' is not one of ['elephant', 'lion']",
        ]
    ]


def test_missing_required_field_is_reported():
    errors = validate_events(_fake(), [_ev("animal_sighting", count=1)])
    assert errors == [["species: required field is missing"]]


def test_v1_type_uses_the_v1_rendered_schema_and_rejects_unknown_keys():
    errors = validate_events(
        _fake(), [_ev("fence_break", fence_name="east", repaired="yes", gate="A")]
    )
    assert errors == [
        [
            "fence_name: 'east' is not one of ['north', 'south']",
            "gate: not a field of 'fence_break'",
            "repaired: 'yes' is not of type 'boolean'",
        ]
    ]


def test_schema_is_fetched_once_per_type():
    fake = _fake()
    validate_events(
        fake,
        [_ev("animal_sighting", species="lion"), _ev("animal_sighting", species="elephant")]
        + [_ev("fence_break", fence_name="north")] * 3,
    )
    schema_gets = [c[1] for c in fake.calls if c[0] == "_get" and "schema" in c[1]]
    assert schema_gets == [
        "activity/eventtypes/animal_sighting/schema",
        "activity/events/schema/eventtype/fence_break",
    ]
    # the v2 rendered schema is requested with choices inlined as enums
    v2_call = next(c for c in fake.calls if c[1] == "activity/eventtypes/animal_sighting/schema")
    assert v2_call[2] == {"pre_render": "true", "s_format": "enum"}
    assert v2_call[3] == "https://fake.pamdas.org/api/v2.0"


def test_unknown_event_type_raises_before_validating_anything():
    fake = _fake()

    def missing(path, base_url=None, params=None, max_retries=5, **kw):
        if path == "activity/eventtypes/nope":
            raise ERClientNotFound()
        return FakeER._get(fake, path, base_url=base_url, params=params, max_retries=max_retries)

    fake._get = missing
    with pytest.raises(UnknownEventType, match="no event type with value 'nope'"):
        validate_events(fake, [_ev("nope", a=1)])


def test_event_without_details_is_checked_against_required_fields():
    errors = validate_events(_fake(), [{"event_type": "animal_sighting"}])
    assert errors == [["species: required field is missing"]]


def test_type_with_no_schema_accepts_anything():
    fake = _fake()
    fake.responses["activity/eventtypes/empty"] = {"value": "empty", "version": "2"}
    fake.responses["activity/eventtypes/empty/schema"] = None
    assert validate_events(fake, [_ev("empty", anything="goes")]) == [[]]
