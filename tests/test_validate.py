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

# A v1 type is NOT served by the v2 detail endpoint (das filters it to v2);
# the CLI falls back to GET /api/v1.0/activity/events/schema/eventtype/fence_break,
# the jinja-rendered v1 envelope with inline enums.
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
    gets = [c[1] for c in fake.calls if c[0] == "_get"]
    assert gets == [
        "activity/eventtypes/animal_sighting",
        "activity/eventtypes/animal_sighting/schema",
        "activity/eventtypes/fence_break",  # 404: not a v2 type
        "activity/events/schema/eventtype/fence_break",  # so resolved as v1
    ]
    # the v2 rendered schema is requested with choices inlined as enums
    v2_call = next(c for c in fake.calls if c[1] == "activity/eventtypes/animal_sighting/schema")
    assert v2_call[2] == {"pre_render": "true", "s_format": "enum"}
    assert v2_call[3] == "https://fake.pamdas.org/api/v2.0"


def test_unknown_event_type_raises_before_validating_anything():
    fake = _fake()

    def missing(path, base_url=None, params=None, max_retries=5, **kw):
        if path.endswith("/nope"):  # neither a v2 type nor a v1 type
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


@pytest.mark.parametrize("bad", [None, 7, {"value": "x"}, "", ["a"]])
def test_non_string_event_type_is_a_clean_error(bad):
    with pytest.raises(UnknownEventType, match="event_type must be a non-empty string"):
        validate_events(_fake(), [{"event_type": bad, "event_details": {}}])


@pytest.mark.parametrize("details", [[], False, 0, "", None, [1, 2], "x"])
@pytest.mark.parametrize("schema", [None, V2_RENDERED])
def test_non_object_details_are_rejected_with_or_without_a_schema(details, schema):
    # das refuses every one of these; the message must not depend on whether
    # the type happens to have a schema
    fake = _fake()
    fake.responses["activity/eventtypes/animal_sighting/schema"] = schema
    errors = validate_events(fake, [{"event_type": "animal_sighting", "event_details": details}])
    assert errors == [[f"event_details must be an object of field values, got {details!r}"]]


@pytest.mark.parametrize(
    "schema", [None, {"json": {"type": "object", "properties": {"count": {"type": "number"}}}}]
)
def test_null_details_are_rejected_even_without_required_fields_or_schema(schema):
    fake = _fake()
    fake.responses["activity/eventtypes/animal_sighting/schema"] = schema
    event = {"event_type": "animal_sighting", "event_details": None}
    assert validate_events(fake, [event]) == [
        ["event_details must be an object of field values, got None"]
    ]
    assert event["event_details"] is None


def test_omitted_details_are_accepted_without_required_fields():
    fake = _fake()
    fake.responses["activity/eventtypes/animal_sighting/schema"] = {
        "json": {"type": "object", "properties": {"count": {"type": "number"}}}
    }
    assert validate_events(fake, [{"event_type": "animal_sighting"}]) == [[]]


# A v2 type with a collection, a conditional section holding another
# collection, a location (fixed shape) and an attachment field — rendered as
# das serves it: strictness stripped on the root and on both collections.
V2_NESTED = {
    "json": {
        "type": "object",
        "properties": {
            "animals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "count": {"type": "number"},
                        "species": {"type": "string", "enum": ["lion", "elephant"]},
                    },
                    "required": ["species"],
                },
            },
            "where": {
                "type": "object",
                "properties": {"latitude": {"type": "number"}, "longitude": {"type": "number"}},
                "unevaluatedProperties": False,
            },
            "photos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"uploadId": {"type": "string"}},
                    "unevaluatedProperties": False,
                },
            },
            "has_injuries": {"type": "boolean"},
        },
        "allOf": [
            {
                "if": {
                    "properties": {"has_injuries": {"const": True}},
                    "required": ["has_injuries"],
                },
                "then": {
                    "properties": {
                        "injuries": {
                            "type": "array",
                            "items": {"type": "object", "properties": {"kind": {"type": "string"}}},
                        }
                    }
                },
            }
        ],
    }
}


def _nested_fake():
    fake = FakeER()
    fake.responses["activity/eventtypes/nested"] = {"value": "nested", "version": "2"}
    fake.responses["activity/eventtypes/nested/schema"] = V2_NESTED
    return fake


def test_unknown_keys_inside_collection_items_are_errors_with_their_path():
    errors = validate_events(
        _nested_fake(),
        [_ev("nested", animals=[{"species": "lion"}, {"species": "lion", "coutn": 2}])],
    )
    assert errors == [["animals.1.coutn: not a field of 'nested'"]]


def test_missing_required_inside_collection_item_names_the_item():
    errors = validate_events(_nested_fake(), [_ev("nested", animals=[{"count": 2}])])
    assert errors == [["animals.0.species: required field is missing"]]


def test_conditional_section_collection_items_are_strict_too():
    errors = validate_events(
        _nested_fake(),
        [_ev("nested", has_injuries=True, injuries=[{"kind": "leg"}, {"knid": "tail"}])],
    )
    assert errors == [["injuries.1.knid: not a field of 'nested'"]]
    # and the section's own field is accepted when the condition holds
    assert validate_events(_nested_fake(), [_ev("nested", has_injuries=True, injuries=[])]) == [[]]


def test_fixed_shape_objects_keep_their_own_rules():
    # location-like objects and attachment items are not collections: das
    # leaves their strictness alone, and so do we — they validate as written
    errors = validate_events(
        _nested_fake(),
        [
            _ev(
                "nested",
                where={"latitude": 1, "longitude": 2, "altitude": 3},
                photos=[{"uploadId": "u", "x": 1}],
            )
        ],
    )
    assert errors == [
        ["photos.0.x: not a field of 'nested'", "where.altitude: not a field of 'nested'"]
    ]


def test_schema_fetch_body_failure_is_retried(monkeypatch, capsys):
    from requests.exceptions import ChunkedEncodingError
    from urllib3.exceptions import ProtocolError

    from earthranger_cli import read

    monkeypatch.setattr(read, "_sleep", lambda s: None)
    fake = _fake()
    real_get = fake._get
    dropped = {"left": 1}

    def flaky(path, base_url=None, params=None, max_retries=5, **kw):
        if path.endswith("/schema") and dropped["left"]:
            dropped["left"] -= 1
            raise ChunkedEncodingError(ProtocolError("Connection broken: IncompleteRead"))
        return real_get(path, base_url=base_url, params=params, max_retries=max_retries)

    fake._get = flaky
    errors = validate_events(fake, [_ev("animal_sighting", species="lion")])
    assert errors == [[]]
    assert "retrying (1 of 3)" in capsys.readouterr().err
    assert dropped["left"] == 0  # the drop was consumed, then the schema came through
