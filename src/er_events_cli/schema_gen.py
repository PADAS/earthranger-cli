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
    name = choice_field_name(event_type_value, field.key)
    ref = _REF_TEMPLATE.format(field=name)
    ui_field = {
        "type": "CHOICE_LIST",
        "inputType": "DROPDOWN",
        "placeholder": "",
        "choices": {
            "type": "EXISTING_CHOICE_LIST",
            "existingChoiceList": [name],
            "eventTypeCategories": [],
            "featureCategories": [],
            "myDataType": "",
            "subjectGroups": [],
            "subjectSubtypes": [],
        },
        "parent": SECTION_ID,
    }
    if field.type == "multiselect":
        json_prop = {
            "type": "array",
            "title": field.label,
            "uniqueItems": True,
            "items": {"type": "string", "anyOf": [{"$ref": ref}]},
        }
    else:
        json_prop = {"type": "string", "title": field.label, "anyOf": [{"$ref": ref}]}
    return json_prop, ui_field


def build_schema(et: EventTypeSpec) -> dict:
    properties: dict[str, dict] = {}
    ui_fields: dict[str, dict] = {}
    for f in et.fields:
        json_prop, ui_field = build_property_pair(f, et.value)
        properties[f.key] = json_prop
        ui_fields[f.key] = ui_field
    return {
        "json": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "unevaluatedProperties": False,
            "properties": properties,
            "required": list(et.required),
        },
        "ui": {
            "fields": ui_fields,
            "headers": {},
            "order": [SECTION_ID],
            "sections": {
                SECTION_ID: {
                    "label": "Details",
                    "columns": 1,
                    "isActive": True,
                    "leftColumn": [{"name": f.key, "type": "field"} for f in et.fields],
                    "rightColumn": [],
                }
            },
        },
    }


def build_event_type_payload(et: EventTypeSpec, category_value: str) -> dict:
    payload = {
        "value": et.value,
        "display": et.display,
        "category": category_value,
        "is_active": et.is_active,
        "readonly": False,
        "schema": build_schema(et),
    }
    if et.icon_id:
        payload["icon_id"] = et.icon_id
    return payload
