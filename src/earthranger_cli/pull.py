"""Reconstruct a DSL spec from server state — the inverse of schema_gen.

Only shapes our generator can produce are invertible. Anything else (ER
builder constructs like collections, locations, multi-section layouts, or
choice fields with hand-picked names) is reported in PullResult.unsupported
instead of being silently mangled: applying a lossy pull would rewrite the
server object, so the caller decides whether to drop those pieces.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import yaml

from . import client as er
from .choices import choice_sort_key
from .dsl import CHOICES_FIELD_RE, PRIORITY_BY_VALUE
from .schema_gen import choice_field_name

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

    shared_sets = _hoist_shared_sets(event_types)
    spec = {"category": {"value": category_value, "display": cat.get("display")}}
    if shared_sets:
        spec["choices"] = shared_sets
    spec["event_types"] = event_types
    return PullResult(spec=spec, unsupported=unsupported)


def _hoist_shared_sets(event_types: list[dict]) -> dict:
    """A choices_field referenced by more than one field becomes a top-level
    choices: set (options identical by construction — one server set)."""
    counts: dict[str, int] = {}
    for et in event_types:
        for f in _choice_fields_of(et):
            name = f.get("choices_field")
            if name:
                counts[name] = counts.get(name, 0) + 1
    shared: dict = {}
    for et in event_types:
        for f in _choice_fields_of(et):
            name = f.get("choices_field")
            if name and counts[name] > 1:
                shared.setdefault(name, f.get("options", []))
                f.pop("options", None)
    return shared


def _choice_fields_of(et: dict):
    for section in et.get("sections") or []:
        yield from (f for f in section.get("fields", []) if "choices_field" in f)
    yield from (f for f in et.get("fields") or [] if "choices_field" in f)


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
    if isinstance(schema, str):
        # ER sometimes returns schemas JSON-stringified on GET.
        try:
            schema = json.loads(schema)
        except (json.JSONDecodeError, TypeError):
            schema = None
    if isinstance(schema, dict) and schema.get("auto-generate"):
        unsupported.append(f"event type {value!r}: schema uses auto-generate; skipped entirely")
        return None
    if (
        not isinstance(schema, dict)
        or not isinstance(schema.get("json"), dict)
        or not isinstance(schema.get("ui"), dict)
    ):
        unsupported.append(
            f"event type {value!r}: schema is not a v2 json/ui envelope "
            "(a v1 event type?); skipped entirely"
        )
        return None
    json_block = schema.get("json") or {}
    ui_block = schema.get("ui") or {}
    properties = json_block.get("properties") or {}
    ui_fields = ui_block.get("fields") or {}

    if not properties and not (ui_block.get("sections") or {}):
        # a form-less event type (e.g. an incident collection): no fields at all
        out: dict = {"value": value, "display": et.get("display")}
        if et.get("is_active", True) is False:
            out["is_active"] = False
        if et.get("icon"):
            out["icon_id"] = et["icon"]
        if et.get("is_collection"):
            out["is_collection"] = True
        _copy_type_defaults(et, out)
        out["fields"] = []
        return out

    sections_result = _read_sections(json_block, ui_block, properties, ui_fields)
    if isinstance(sections_result, str):
        unsupported.append(f"event type {value!r}: {sections_result}; skipped entirely")
        return None

    out_sections: list[dict] = []
    kept_keys: set[str] = set()
    for section in sections_result:
        sec_fields: list[dict] = []
        for key in section["order"]:
            f, reason = _invert_field(client, value, key, properties[key], ui_fields.get(key) or {})
            if f is None:
                unsupported.append(f"event type {value!r}, field {key!r}: {reason}; skipped")
                continue
            if key in section["right"]:
                f["column"] = "right"
            sec_fields.append(f)
            kept_keys.add(key)
        if not sec_fields:
            # a fieldless section has no DSL form; dropping it shifts the
            # positional ids of later sections, so this stays lossy territory
            unsupported.append(
                f"event type {value!r}: layout section {section['label']!r} kept no fields; dropped"
            )
            continue
        sec_out: dict = {}
        if section["label"] != "Details":
            sec_out["label"] = section["label"]
        if section["columns"] != 1:
            sec_out["columns"] = section["columns"]
        sec_out["fields"] = sec_fields
        out_sections.append(sec_out)

    if not out_sections:
        unsupported.append(f"event type {value!r}: no fields could be expressed; skipped entirely")
        return None

    out: dict = {"value": value, "display": et.get("display")}
    if et.get("is_active", True) is False:
        out["is_active"] = False
    if et.get("icon"):
        out["icon_id"] = et["icon"]
    if et.get("is_collection"):
        out["is_collection"] = True
    _copy_type_defaults(et, out)
    if len(out_sections) == 1:
        single = out_sections[0]
        layout = {
            "label": single.get("label", "Details"),
            "columns": single.get("columns", 1),
        }
        if layout != {"label": "Details", "columns": 1}:
            out["layout"] = layout
        out["fields"] = single["fields"]
    else:
        out["sections"] = out_sections
    required = [k for k in (json_block.get("required") or []) if k in kept_keys]
    if required:
        out["required"] = required
    return out


def _read_sections(json_block, ui_block, properties, ui_fields):
    """Return a list of {label, columns, order, right} for a sectioned schema
    the DSL can express (positional section-N ids, no headers/conditions), or a
    reason string when it cannot."""
    sections = ui_block.get("sections") or {}
    order_ids = ui_block.get("order") or []
    if not sections:
        return "layout uses 0 sections"
    if sorted(sections) != sorted(order_ids):
        return "layout sections do not match ui.order"
    expected_ids = [f"section-{i}" for i in range(1, len(order_ids) + 1)]
    if order_ids != expected_ids:
        return f"layout section ids {order_ids!r} are not positional {expected_ids!r}"
    if ui_block.get("headers"):
        return "layout uses headers"
    if json_block.get("allOf"):
        return "layout uses conditional sections"

    out: list[dict] = []
    all_keys: list[str] = []
    for sid in order_ids:
        section = sections[sid]
        if section.get("isActive") is not True:
            return f"layout section {sid!r} is inactive (isActive: false)"
        if section.get("conditions"):
            return "layout uses section conditions"
        label = section.get("label")
        columns = section.get("columns")
        if not isinstance(label, str) or columns not in (1, 2):
            return f"layout section {sid!r} has label={label!r}, columns={columns!r}"
        left = [e.get("name") for e in section.get("leftColumn") or [] if e.get("type") == "field"]
        right = [
            e.get("name") for e in section.get("rightColumn") or [] if e.get("type") == "field"
        ]
        if columns == 1 and right:
            return f"layout section {sid!r} puts fields in the right column of a 1-column section"
        all_keys.extend(left + right)
        out.append({"label": label, "columns": columns, "order": left + right, "right": set(right)})
    if set(all_keys) != set(properties) or set(all_keys) != set(ui_fields):
        return "layout leaves fields outside the sections"
    return out


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
        # A stock/hand-built type owns its choice-field name; carry it as an
        # explicit choices_field so apply keeps using it instead of renaming.
        if not CHOICES_FIELD_RE.match(name):
            return None, (
                f"choice field name {name!r} cannot be expressed as a choices_field "
                "(character set or length)"
            )
        out["choices_field"] = name
    options = []
    for record in sorted(er.get_choices(client, name), key=choice_sort_key):
        if not record.get("is_active", True):
            continue
        value, display = record.get("value"), record.get("display")
        icon = record.get("icon") or None
        if icon is None and display == value.replace("_", " ").title():
            options.append(value)  # shorthand round-trips to the same display
        else:
            opt = {"value": value, "display": display}
            if icon:
                opt["icon"] = icon
            options.append(opt)
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


def _copy_type_defaults(et: dict, out: dict) -> None:
    """Emit default_priority/default_state/readonly only when non-default, so
    minimal specs stay minimal and omitted keys keep preserving server values."""
    pri = et.get("default_priority")
    if pri:
        out["default_priority"] = PRIORITY_BY_VALUE.get(pri, pri)
    state = et.get("default_state")
    if state and state != "new":
        out["default_state"] = state
    if et.get("readonly"):
        out["readonly"] = True
