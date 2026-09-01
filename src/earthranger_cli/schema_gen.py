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


def effective_choice_field(event_type_value: str, field: FieldSpec) -> str:
    """The Choice.field name a select/multiselect actually uses: an explicit
    choices_field (stock ER types predate our derivation) or the derived name."""
    return field.choices_field or choice_field_name(event_type_value, field.key)


# DSL format values -> JSON Schema format strings (ER's builder calls uri "URL").
_FORMAT_WIRE = {"url": "uri", "email": "email", "uuid": "uuid"}


def build_property_pair(field: FieldSpec, event_type_value: str) -> tuple[dict, dict]:
    if field.type in _SCALAR_JSON:
        # ER's meta-schema requires "deprecated" on every json property.
        json_prop = {
            **_SCALAR_JSON[field.type],
            "title": field.label,
            "deprecated": not field.active,
        }
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
    name = effective_choice_field(event_type_value, field)
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
            "deprecated": not field.active,
            "uniqueItems": True,
            "items": {"type": "string", "anyOf": [{"$ref": ref}]},
        }
    else:
        json_prop = {
            "type": "string",
            "title": field.label,
            "deprecated": not field.active,
            "anyOf": [{"$ref": ref}],
        }
    if field.description is not None:
        json_prop["description"] = field.description
    return json_prop, ui_field


def build_schema(et: EventTypeSpec) -> dict:
    properties: dict[str, dict] = {}
    ui_fields: dict[str, dict] = {}
    sections: dict[str, dict] = {}
    order: list[str] = []
    all_of: list[dict] = []
    required = list(et.required)
    # a field-less event type (e.g. an incident collection) has NO sections at
    # all on ER, so the empty sugar section is skipped rather than emitted
    real_sections = [sec for sec in (et.sections or []) if sec.fields]
    for idx, sec in enumerate(real_sections, start=1):
        sid = f"section-{idx}"
        order.append(sid)
        left: list[dict] = []
        right: list[dict] = []
        sec_props: dict[str, dict] = {}
        for f in sec.fields:
            json_prop, ui_field = build_property_pair(f, et.value)
            ui_field["parent"] = sid
            sec_props[f.key] = json_prop
            ui_fields[f.key] = ui_field
            (right if f.column == "right" else left).append({"name": f.key, "type": "field"})
        section_ui = {
            "label": sec.label,
            "columns": sec.columns,
            "isActive": sec.active,
            "leftColumn": left,
            "rightColumn": right,
        }
        if sec.condition is None:
            properties.update(sec_props)
        else:
            # a conditional section's fields live only inside the allOf branch
            sec_required = [k for k in required if k in sec_props]
            required = [k for k in required if k not in sec_props]
            all_of.append(
                {
                    "if": _encode_is_exactly(sec.condition.field, sec.condition.value),
                    "then": {"properties": sec_props, "required": sec_required},
                    "x-section": sid,
                }
            )
            section_ui["conditions"] = [
                {
                    "field": sec.condition.field,
                    # deterministic so re-applies are idempotent; the server's
                    # own random ids are canonicalized away in the diff
                    "id": f"condition-{sid}-1",
                    "operator": "IS_EXACTLY",
                    "value": sec.condition.value,
                }
            ]
        sections[sid] = section_ui
    for sec_idx, sec in enumerate(real_sections, start=1):
        if sec.condition is not None:
            controller = ui_fields.get(sec.condition.field)
            if controller is not None:
                controller.setdefault("conditionalDependents", []).append(f"section-{sec_idx}")
    json_block = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "unevaluatedProperties": False,
        "properties": properties,
        "required": required,
    }
    if all_of:
        json_block["allOf"] = all_of
    return {
        "json": json_block,
        "ui": {
            "fields": ui_fields,
            "headers": {},
            "order": order,
            "sections": sections,
        },
    }


def _encode_is_exactly(field_key: str, value: str) -> dict:
    """ER's builder expands the IS_EXACTLY triple into this anyOf matrix over
    the value shapes a property can take; replicated verbatim from live data
    and validated against das's is_exactly_condition_json_schema."""
    return {
        "allOf": [
            {
                "properties": {
                    field_key: {
                        "anyOf": [
                            {
                                "allOf": [{"contains": {"const": value}}],
                                "maxItems": 1,
                                "type": "array",
                            },
                            {"const": None, "type": "boolean"},
                            {"const": None, "type": "number"},
                            {
                                "properties": {value: {}},
                                "required": [value],
                                "type": "object",
                                "unevaluatedProperties": False,
                            },
                            {"const": value, "type": "string"},
                        ]
                    }
                },
                "required": [field_key],
            }
        ]
    }


def build_event_type_payload(et: EventTypeSpec, category_value: str) -> dict:
    payload = {
        "value": et.value,
        "display": et.display,
        "category": category_value,
        "is_active": et.is_active,
        "schema": build_schema(et),
    }
    if et.icon_id:
        # ER's v2 API treats icon_id as read-only (a property derived from the
        # EventType.icon model field); "icon" is the writable field.
        payload["icon"] = et.icon_id
    if et.is_collection:
        payload["is_collection"] = True
    # sent only when declared: creates get server defaults, patches preserve
    if et.default_priority is not None:
        payload["default_priority"] = et.default_priority
    if et.default_state is not None:
        payload["default_state"] = et.default_state
    if et.readonly is not None:
        payload["readonly"] = et.readonly
    if et.geometry_type is not None:
        payload["geometry_type"] = et.geometry_type
    if et.auto_resolve is not None:
        payload["auto_resolve"] = et.auto_resolve
    if et.resolve_time is not None:
        payload["resolve_time"] = et.resolve_time
    if et.ordernum is not None:
        payload["ordernum"] = et.ordernum
    return payload
