"""Reconstruct a DSL spec from server state — the inverse of schema_gen.

Only shapes our generator can produce are invertible. Anything else (ER
builder constructs like collections, locations, multi-section layouts, or
choice fields with hand-picked names) is reported in PullResult.unsupported
instead of being silently mangled: applying a lossy pull would rewrite the
server object, so the caller decides whether to drop those pieces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from . import client as er
from .schema_gen import SECTION_ID, choice_field_name

_REF_FIELD_RE = re.compile(r"choices\.json\?field=([^&\"']+)$")
# JSON Schema format -> DSL: uri gets its own field type, the rest are formats.
_FORMAT_DSL = {"email": "email", "uuid": "uuid"}


class PullError(Exception):
    pass


@dataclass
class PullResult:
    spec: dict
    unsupported: list[str] = field(default_factory=list)


def pull_category(client, category_value: str) -> PullResult:
    categories = client.get_event_categories(include_inactive=True)
    cat = next((c for c in categories if c.get("value") == category_value), None)
    if cat is None:
        raise PullError(f"no category with value {category_value!r} on the server")

    unsupported: list[str] = []
    event_types: list[dict] = []
    all_types = client.get_event_types(include_inactive=True, include_schema=True, version="v2.0")
    for et in all_types:
        if _category_value(et.get("category")) != category_value:
            continue
        inverted = _invert_event_type(client, et, unsupported)
        if inverted is not None:
            event_types.append(inverted)

    spec = {
        "category": {"value": category_value, "display": cat.get("display")},
        "event_types": event_types,
    }
    return PullResult(spec=spec, unsupported=unsupported)


def render_spec_yaml(result: PullResult) -> str:
    header = ""
    if result.unsupported:
        lines = "\n".join(f"#   - {w}" for w in result.unsupported)
        header = f"# Skipped constructs the DSL cannot express:\n{lines}\n"
    body = yaml.safe_dump(result.spec, sort_keys=False, allow_unicode=True, width=88)
    return header + body


def _category_value(raw: object) -> str | None:
    if isinstance(raw, dict):
        return raw.get("value")
    return raw


def _invert_event_type(client, et: dict, unsupported: list[str]) -> dict | None:
    value = et.get("value") or ""
    schema = et.get("schema") or {}
    json_block = schema.get("json") or {}
    ui_block = schema.get("ui") or {}
    properties = json_block.get("properties") or {}
    ui_fields = ui_block.get("fields") or {}

    order = _field_order(json_block, ui_block, properties, ui_fields)
    if order is None:
        unsupported.append(
            f"event type {value!r}: layout is not the generator's single "
            f"'{SECTION_ID}' Details section; skipped entirely"
        )
        return None

    fields: list[dict] = []
    kept_keys: set[str] = set()
    for key in order:
        f, reason = _invert_field(client, value, key, properties[key], ui_fields.get(key) or {})
        if f is None:
            unsupported.append(f"event type {value!r}, field {key!r}: {reason}; skipped")
            continue
        fields.append(f)
        kept_keys.add(key)

    if not fields:
        unsupported.append(f"event type {value!r}: no fields could be expressed; skipped entirely")
        return None

    out: dict = {"value": value, "display": et.get("display")}
    if et.get("is_active", True) is False:
        out["is_active"] = False
    if et.get("icon"):
        out["icon_id"] = et["icon"]
    out["fields"] = fields
    required = [k for k in (json_block.get("required") or []) if k in kept_keys]
    if required:
        out["required"] = required
    return out


def _field_order(json_block, ui_block, properties, ui_fields) -> list[str] | None:
    """Field order from the single Details section, or None when the layout is
    something our generator never produces (multi-section, headers, conditions,
    right column, or fields outside the section)."""
    sections = ui_block.get("sections") or {}
    if list(sections) != [SECTION_ID] or ui_block.get("headers") or json_block.get("allOf"):
        return None
    section = sections[SECTION_ID]
    if section.get("rightColumn") or section.get("conditions"):
        return None
    if section.get("label") != "Details" or section.get("columns") != 1:
        return None
    order = [e.get("name") for e in section.get("leftColumn") or [] if e.get("type") == "field"]
    if set(order) != set(properties) or set(order) != set(ui_fields):
        return None
    return order


def _invert_field(client, et_value, key, json_prop, ui_field):
    """Return (dsl_field_dict, None) or (None, reason)."""
    if json_prop.get("deprecated"):
        return None, "deprecated fields have no DSL equivalent"
    ui_type = ui_field.get("type")
    out: dict = {"key": key, "label": json_prop.get("title")}

    if ui_type == "CHOICE_LIST":
        return _invert_choice_field(client, et_value, key, json_prop, ui_field, out)
    if ui_type == "TEXT":
        if "pattern" in json_prop:
            return None, "pattern validation has no DSL equivalent"
        fmt = json_prop.get("format")
        if ui_field.get("inputType") == "LONG_TEXT":
            if fmt:
                return None, f"textarea with format {fmt!r} has no DSL equivalent"
            out["type"] = "textarea"
        elif fmt == "uri":
            out["type"] = "url"
        elif fmt in _FORMAT_DSL:
            out["type"] = "string"
            out["format"] = _FORMAT_DSL[fmt]
        elif fmt:
            return None, f"format {fmt!r} has no DSL equivalent"
        else:
            out["type"] = "string"
    elif ui_type == "NUMERIC":
        out["type"] = "number"
        if "minimum" in json_prop:
            out["min"] = json_prop["minimum"]
        if "maximum" in json_prop:
            out["max"] = json_prop["maximum"]
    elif ui_type == "BOOLEAN":
        out["type"] = "boolean"
    elif ui_type == "DATE_TIME":
        fmt = json_prop.get("format")
        if fmt == "date":
            out["type"] = "date"
        elif fmt == "date-time":
            out["type"] = "datetime"
        else:
            return None, f"date/time format {fmt!r} has no DSL equivalent"
    else:
        return None, f"UI field type {ui_type!r} has no DSL equivalent"

    _copy_extras(json_prop, ui_field, out)
    return out, None


def _invert_choice_field(client, et_value, key, json_prop, ui_field, out):
    if json_prop.get("type") == "array":
        out["type"] = "multiselect"
        refs = (json_prop.get("items") or {}).get("anyOf") or []
    else:
        out["type"] = "select"
        refs = json_prop.get("anyOf") or []
    if len(refs) != 1 or "$ref" not in refs[0]:
        return None, "choice list does not reference exactly one choices $ref"
    m = _REF_FIELD_RE.search(refs[0]["$ref"])
    if not m:
        return None, f"unrecognized $ref {refs[0]['$ref']!r}"
    name = m.group(1)
    if name != choice_field_name(et_value, key):
        return None, (
            f"choice field name {name!r} does not match the derived "
            f"'{choice_field_name(et_value, key)}'; applying would rename the choice set"
        )
    options = []
    for record in er.get_choices(client, name):
        if not record.get("is_active", True):
            continue
        value, display = record.get("value"), record.get("display")
        if display == value.replace("_", " ").title():
            options.append(value)  # shorthand round-trips to the same display
        else:
            options.append({"value": value, "display": display})
    out["options"] = options
    _copy_extras(json_prop, ui_field, out)
    return out, None


def _copy_extras(json_prop: dict, ui_field: dict, out: dict) -> None:
    if ui_field.get("placeholder"):
        out["hint"] = ui_field["placeholder"]
    if json_prop.get("description"):
        out["description"] = json_prop["description"]
    if "default" in json_prop:
        out["default"] = json_prop["default"]
