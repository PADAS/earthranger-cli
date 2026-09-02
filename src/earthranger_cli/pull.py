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
from .schema_gen import _encode_is_exactly, choice_field_name

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
    extra_required: list[str] = []
    for section in sections_result:
        sec_props = section["props"]
        extra_required.extend(section["extra_required"])
        sec_fields: list[dict] = []
        for key in section["order"]:
            f, reason = _invert_field(
                client, value, key, sec_props[key], ui_fields.get(key) or {}, ui_fields
            )
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
        if not section["active"]:
            sec_out["active"] = False
        if section["condition"] is not None:
            sec_out["condition"] = section["condition"]
        sec_out["fields"] = sec_fields
        out_sections.append(sec_out)

    for sec_out in out_sections:
        cond = sec_out.get("condition")
        if cond and cond.get("field") not in kept_keys:
            unsupported.append(
                f"event type {value!r}: conditional section {sec_out.get('label')!r} "
                f"references controlling field {cond.get('field')!r}, which was "
                "skipped; condition dropped"
            )
            del sec_out["condition"]

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
    if (
        len(out_sections) == 1
        and "condition" not in out_sections[0]
        and "active" not in out_sections[0]
    ):
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
    required = [
        k for k in list(json_block.get("required") or []) + extra_required if k in kept_keys
    ]
    if required:
        out["required"] = required
    return out


def _read_sections(json_block, ui_block, properties, ui_fields):
    """Return a list of section dicts (label, columns, order, right, active,
    condition, props, extra_required) for a sectioned schema the DSL can
    express, or a reason string when it cannot."""
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

    # conditional sections: each json.allOf block is tied to a section by
    # x-section and carries that section's fields in then.properties
    branches: dict[str, dict] = {}
    for block in json_block.get("allOf") or []:
        sid = block.get("x-section")
        then = block.get("then") or {}
        if not isinstance(sid, str) or not isinstance(then.get("properties"), dict):
            return "layout uses a conditional block the DSL cannot express"
        extra_block = set(block) - {"if", "then", "x-section"}
        extra_then = set(then) - {"properties", "required"}
        if extra_block or extra_then:
            keywords = ", ".join(sorted(extra_block | extra_then))
            return (
                f"layout conditional block for {sid!r} carries keywords the DSL "
                f"cannot express ({keywords})"
            )
        if sid in branches:
            return f"layout has duplicate conditional branches for {sid!r}"
        if sid not in sections:
            return f"layout has a conditional branch for unknown section {sid!r}"
        branches[sid] = {**then, "_if": block.get("if")}

    out: list[dict] = []
    all_keys: list[str] = []
    for sid in order_ids:
        section = sections[sid]
        conditions = section.get("conditions") or []
        condition = None
        if conditions:
            if len(conditions) > 1:
                return f"layout section {sid!r} has multiple conditions"
            cond = conditions[0]
            extra_keys = set(cond) - {"field", "id", "operator", "value"}
            if extra_keys:
                return (
                    f"layout section {sid!r} condition carries keys the DSL cannot "
                    f"express ({', '.join(sorted(extra_keys))})"
                )
            if not isinstance(cond.get("field"), str) or not cond.get("field"):
                return f"layout section {sid!r} condition has no field"
            if not isinstance(cond.get("value"), str) or not cond.get("value"):
                return f"layout section {sid!r} condition has an empty value"
            if cond.get("operator") != "IS_EXACTLY":
                return (
                    f"layout section {sid!r} condition operator "
                    f"{cond.get('operator')!r} is not supported yet"
                )
            if sid not in branches:
                return f"layout section {sid!r} has a condition but no allOf branch"
            expected_if = _encode_is_exactly(cond.get("field"), cond.get("value"))
            if branches[sid].get("_if") != expected_if:
                return f"layout section {sid!r} conditional branch does not match its UI condition"
            condition = {
                "field": cond.get("field"),
                "operator": "is_exactly",
                "value": cond.get("value"),
            }
        elif sid in branches:
            return f"layout has an allOf branch for unconditioned section {sid!r}"
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
        branch = branches.get(sid) or {}
        if condition is not None and set(left + right) != set(branch.get("properties") or {}):
            return (
                f"layout section {sid!r} conditional branch properties do not "
                "match its section columns"
            )
        out.append(
            {
                "label": label,
                "columns": columns,
                "order": left + right,
                "right": set(right),
                "active": section.get("isActive") is not False,
                "condition": condition,
                "props": branch.get("properties") if condition else properties,
                "extra_required": list(branch.get("required") or []),
            }
        )
    # requiredness must keep its scope: the generator routes required keys to
    # where their fields live, so cross-scope entries would silently change
    # from conditional to global (or vice versa) on re-apply
    for r in json_block.get("required") or []:
        if r not in properties:
            return (
                f"top-level required entry {r!r} is not a top-level property "
                "(requiredness would change scope)"
            )
    for sid, then in branches.items():
        for r in then.get("required") or []:
            if not isinstance(r, str) or r not in (then.get("properties") or {}):
                return (
                    f"conditional branch for {sid!r} requires {r!r}, which is not "
                    "one of its own properties (requiredness would change scope)"
                )

    # conditionalDependents must mirror the conditions exactly: the generator
    # rebuilds them from the conditions, so any divergence (missing linkage,
    # orphaned or extra dependents) would be silently rewritten on apply
    expected_dependents: dict[str, list[str]] = {}
    for sid, sec in zip(order_ids, out):
        cond = sec["condition"]
        if cond is not None:
            expected_dependents.setdefault(cond["field"], []).append(sid)
    for fkey, f in ui_fields.items():
        actual = f.get("conditionalDependents") or []
        if list(actual) != expected_dependents.get(fkey, []):
            return (
                f"ui field {fkey!r} has conditionalDependents {list(actual)!r} that "
                "do not match its conditions"
            )

    # collection sub-fields appear in ui.fields as dotted keys under their
    # collection's key; only dotted keys actually referenced by a collection's
    # columns are exempt from coverage — anything else dotted is orphaned
    collection_columns: set[str] = set()
    for f in ui_fields.values():
        if isinstance(f, dict) and f.get("type") == "COLLECTION":
            collection_columns.update(f.get("leftColumn") or [])
            collection_columns.update(f.get("rightColumn") or [])
    top_ui_keys: set[str] = set()
    for k in ui_fields:
        if "." in k:
            if k not in collection_columns:
                return f"layout leaves dotted UI field {k!r} outside any collection"
        else:
            top_ui_keys.add(k)
    expected = set(properties)
    for then in branches.values():
        expected |= set(then["properties"])
    if set(all_keys) != expected or set(all_keys) != top_ui_keys:
        return "layout leaves fields outside the sections"
    return out


def _invert_field(client, et_value, key, json_prop, ui_field, all_ui_fields=None):
    """Return (dsl_field_dict, None) or (None, reason)."""
    ui_type = ui_field.get("type")
    out: dict = {"key": key, "label": json_prop.get("title")}
    if json_prop.get("deprecated"):
        out["active"] = False

    if ui_type == "COLLECTION":
        return _invert_collection(client, key, json_prop, ui_field, all_ui_fields or {}, out)
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


def _invert_collection(client, key, json_prop, ui_field, all_ui_fields, out):
    out["type"] = "collection"
    item_name = ui_field.get("itemName")
    if not isinstance(item_name, str) or not item_name:
        return None, "collection has no itemName"
    out["item_name"] = item_name
    if ui_field.get("buttonText"):
        out["button_text"] = ui_field["buttonText"]
    if ui_field.get("itemIdentifier"):
        out["item_identifier"] = ui_field["itemIdentifier"]
    if ui_field.get("columns") == 2:
        out["columns"] = 2
    extra_outer = set(json_prop) - {
        "type",
        "title",
        "deprecated",
        "description",
        "items",
        "minItems",
        "maxItems",
        "unevaluatedItems",
    }
    items = json_prop.get("items") or {}
    extra_items = set(items) - {"type", "properties", "required", "unevaluatedProperties"}
    if extra_outer or extra_items:
        keywords = ", ".join(sorted(extra_outer | extra_items))
        return None, f"collection carries keywords the DSL cannot express ({keywords})"
    props = items.get("properties") or {}
    if not props:
        return None, "collection has no sub-fields"
    left = list(ui_field.get("leftColumn") or [])
    right = list(ui_field.get("rightColumn") or [])
    right_set = set(right)
    sub_fields: list[dict] = []
    seen: set[str] = set()
    prefix = f"{key}."
    for dotted in left + right:
        if not isinstance(dotted, str) or not dotted.startswith(prefix):
            return None, f"collection column {dotted!r} does not belong to this collection"
        sub_key = dotted[len(prefix) :]
        if "." in sub_key:
            return None, f"collection column {dotted!r} is nested more than one level"
        if sub_key in seen:
            return None, f"collection has a duplicate column entry for {sub_key!r}"
        seen.add(sub_key)
        sub_json = props.get(sub_key)
        if sub_json is None:
            return None, f"collection sub-field {sub_key!r} has no json property"
        sub_ui = all_ui_fields.get(dotted) or {}
        if sub_ui.get("type") in ("COLLECTION", "CHOICE_LIST"):
            return None, (
                f"collection sub-field {sub_key!r} is a {sub_ui.get('type')}, "
                "which is not supported inside a collection yet"
            )
        sub, reason = _invert_field(client, "", sub_key, sub_json, sub_ui, all_ui_fields)
        if sub is None:
            return None, f"collection sub-field {sub_key!r}: {reason}"
        if dotted in right_set:
            sub["column"] = "right"
        sub_fields.append(sub)
    if seen != set(props):
        return None, "collection layout leaves sub-fields outside its columns"
    if json_prop.get("minItems") is not None:
        out["min"] = json_prop["minItems"]
    if json_prop.get("maxItems") is not None:
        out["max"] = json_prop["maxItems"]
    out["fields"] = sub_fields
    required = list(items.get("required") or [])
    for r in required:
        if not isinstance(r, str) or r not in props:
            return None, (f"collection requires {r!r}, which is not one of its sub-fields")
    if required:
        out["required"] = required
    # collections take description only; hint/default have no DSL form here
    if ui_field.get("placeholder"):
        return None, "collection has an unsupported placeholder"
    if json_prop.get("default") not in (None, ""):
        return None, "collection has an unsupported default"
    if json_prop.get("description"):
        out["description"] = json_prop["description"]
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
    if "default" in json_prop and json_prop["default"] != "":
        # default: "" is builder echo noise, wire-equivalent to no default
        out["default"] = json_prop["default"]


def _as_int(value):
    """ER's serializer returns these small integers as floats (30.0)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


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
    geometry = et.get("geometry_type")
    if geometry and geometry != "Point":
        out["geometry_type"] = geometry.lower()
    if et.get("auto_resolve"):
        out["auto_resolve"] = True
    if et.get("resolve_time") is not None:
        out["resolve_time"] = _as_int(et["resolve_time"])
    if et.get("ordernum") is not None:
        out["ordernum"] = _as_int(et["ordernum"])
