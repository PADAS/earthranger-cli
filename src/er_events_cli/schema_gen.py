"""Generate ER v2 event-type schema envelopes from FieldSpecs. Pure functions only."""

from __future__ import annotations

import hashlib

from .dsl import EventTypeSpec, FieldSpec

SECTION_ID = "section-1"
VARCHAR_LIMIT = 100
# ER's Choice.field column is varchar(40) — smaller than the varchar(100)
# on value/display (das/choices/models.py).
FIELD_NAME_LIMIT = 40
_REF_TEMPLATE = "/api/v2.0/schemas/choices.json?field={field}"

_SCALAR_JSON = {
    "string": {"type": "string"},
    "textarea": {"type": "string"},
    # ER's meta-schema has no integer variant (numeric type is const "number"),
    # so integer is advisory in the DSL and emits number on the wire.
    "integer": {"type": "number"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
    "url": {"type": "string", "format": "uri"},
}

_SCALAR_UI = {
    "string": {"type": "TEXT", "inputType": "SHORT_TEXT"},
    "textarea": {"type": "TEXT", "inputType": "LONG_TEXT"},
    "integer": {"type": "NUMERIC"},
    "number": {"type": "NUMERIC"},
    "boolean": {"type": "BOOLEAN"},
    "date": {"type": "DATE_TIME"},
    "datetime": {"type": "DATE_TIME"},
    "url": {"type": "TEXT", "inputType": "SHORT_TEXT"},
}


def choice_field_name(event_type_value: str, field_key: str) -> str:
    """Derive the Choice.field name for a select/multiselect field.

    Names that fit ER's varchar(40) field column are used as-is (readable).
    Longer ones keep a 31-char readable prefix plus an 8-char digest of the
    full name, so distinct long fields stay distinct and re-applies derive
    the same name every time.
    """
    full = f"{event_type_value}_{field_key}"
    if len(full) <= FIELD_NAME_LIMIT:
        return full
    digest = hashlib.sha1(full.encode("utf-8")).hexdigest()[:8]
    return f"{full[: FIELD_NAME_LIMIT - 9]}_{digest}"


# DSL format values -> JSON Schema format strings (ER's builder calls uri "URL").
_FORMAT_WIRE = {"url": "uri", "email": "email", "uuid": "uuid"}


def build_property_pair(field: FieldSpec, event_type_value: str) -> tuple[dict, dict]:
    if field.type in _SCALAR_JSON:
        # ER's meta-schema requires "deprecated" on every json property.
        json_prop = {**_SCALAR_JSON[field.type], "title": field.label, "deprecated": False}
        if field.format:
            json_prop["format"] = _FORMAT_WIRE[field.format]
        if field.min is not None:
            json_prop["minimum"] = field.min
        if field.max is not None:
            json_prop["maximum"] = field.max
        if field.description is not None:
            json_prop["description"] = field.description
        if field.default is not None:
            json_prop["default"] = field.default
        ui_field = {**_SCALAR_UI[field.type], "parent": SECTION_ID}
        if field.hint:
            ui_field["placeholder"] = field.hint
        return json_prop, ui_field
    return _build_choice_pair(field, event_type_value)


def _build_choice_pair(field: FieldSpec, event_type_value: str) -> tuple[dict, dict]:
    name = choice_field_name(event_type_value, field.key)
    ref = _REF_TEMPLATE.format(field=name)
    # ER's meta-schema rejects extra UI keys (the legacy builder-only "choices"
    # block included); the json $ref is the sole linkage to the Choice records.
    ui_field = {"type": "CHOICE_LIST", "inputType": "DROPDOWN", "parent": SECTION_ID}
    if field.hint:
        ui_field["placeholder"] = field.hint
    if field.type == "multiselect":
        json_prop = {
            "type": "array",
            "title": field.label,
            "deprecated": False,
            "uniqueItems": True,
            "items": {"type": "string", "anyOf": [{"$ref": ref}]},
        }
    else:
        json_prop = {
            "type": "string",
            "title": field.label,
            "deprecated": False,
            "anyOf": [{"$ref": ref}],
        }
    if field.description is not None:
        json_prop["description"] = field.description
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
                    "label": et.layout.label,
                    "columns": et.layout.columns,
                    "isActive": True,
                    "leftColumn": [
                        {"name": f.key, "type": "field"} for f in et.fields if f.column != "right"
                    ],
                    "rightColumn": [
                        {"name": f.key, "type": "field"} for f in et.fields if f.column == "right"
                    ],
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
        # ER's v2 API treats icon_id as read-only (a property derived from the
        # EventType.icon model field); "icon" is the writable field.
        payload["icon"] = et.icon_id
    return payload
