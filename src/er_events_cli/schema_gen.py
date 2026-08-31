"""Generate ER v2 event-type schema envelopes from FieldSpecs. Pure functions only."""

from __future__ import annotations

from .dsl import EventTypeSpec, FieldSpec

SECTION_ID = "section-1"
VARCHAR_LIMIT = 100
_REF_TEMPLATE = "/api/v2.0/schemas/choices.json?field={field}"

_SCALAR_JSON = {
    "string": {"type": "string"},
    "textarea": {"type": "string"},
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
}

_SCALAR_UI = {
    "string": {"type": "TEXT", "inputType": "SHORT_TEXT"},
    "textarea": {"type": "TEXT", "inputType": "LONG_TEXT"},
    "integer": {"type": "NUMERIC"},
    "number": {"type": "NUMERIC"},
    "boolean": {"type": "BOOLEAN"},
    "date": {"type": "DATE_TIME"},
    "datetime": {"type": "DATE_TIME"},
}


def choice_field_name(event_type_value: str, field_key: str) -> str:
    return f"{event_type_value}_{field_key}"[:VARCHAR_LIMIT]


def build_property_pair(field: FieldSpec, event_type_value: str) -> tuple[dict, dict]:
    if field.type in _SCALAR_JSON:
        json_prop = {**_SCALAR_JSON[field.type], "title": field.label}
        if field.min is not None:
            json_prop["minimum"] = field.min
        if field.max is not None:
            json_prop["maximum"] = field.max
        return json_prop, {**_SCALAR_UI[field.type], "parent": SECTION_ID}
    return _build_choice_pair(field, event_type_value)


def _build_choice_pair(field: FieldSpec, event_type_value: str) -> tuple[dict, dict]:
    raise NotImplementedError  # Task 4
