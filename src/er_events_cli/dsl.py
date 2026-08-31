"""YAML/JSON spec parsing and validation.

Specs are parsed with yaml.safe_load; YAML is a superset of JSON, so pure-JSON
spec files are accepted as-is (a documented, tested promise).
All problems are collected into one SpecError so users fix everything in one pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

SUPPORTED_TYPES = {
    "string", "textarea", "integer", "number", "boolean",
    "date", "datetime", "select", "multiselect",
}
CHOICE_TYPES = {"select", "multiselect"}
NUMERIC_TYPES = {"integer", "number"}
_SLUG_RE = re.compile(r"^[a-z0-9_]+$")


class SpecError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


@dataclass
class OptionSpec:
    value: str
    display: str


@dataclass
class FieldSpec:
    key: str
    label: str
    type: str
    options: list[OptionSpec] | None = None
    min: float | None = None
    max: float | None = None


@dataclass
class EventTypeSpec:
    value: str
    display: str
    fields: list[FieldSpec]
    required: list[str] = field(default_factory=list)
    is_active: bool = True
    icon_id: str | None = None


@dataclass
class CategorySpec:
    value: str
    display: str


@dataclass
class Spec:
    category: CategorySpec
    event_types: list[EventTypeSpec]


def load_spec(path: str) -> Spec:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return parse_spec(data)


def parse_spec(data: object) -> Spec:
    errors: list[str] = []
    if not isinstance(data, dict):
        raise SpecError(["spec must be a mapping with 'category' and 'event_types' keys"])
    category = _parse_category(data.get("category"), errors)
    event_types = _parse_event_types(data.get("event_types"), errors)
    if errors:
        raise SpecError(errors)
    return Spec(category=category, event_types=event_types)


def _parse_category(raw: object, errors: list[str]) -> CategorySpec:
    if not isinstance(raw, dict):
        errors.append("category: required mapping with 'value' and 'display'")
        return CategorySpec(value="", display="")
    value = _required_slug(raw.get("value"), "category.value", errors)
    display = _required_str(raw.get("display"), "category.display", errors)
    return CategorySpec(value=value, display=display)


def _parse_event_types(raw: object, errors: list[str]) -> list[EventTypeSpec]:
    if not isinstance(raw, list) or not raw:
        errors.append("event_types: at least one event type is required")
        return []
    out: list[EventTypeSpec] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        et = _parse_event_type(item, f"event_types[{i}]", errors)
        if et.value and et.value in seen:
            errors.append(f"event_types[{i}].value: duplicate event type value {et.value!r}")
        seen.add(et.value)
        out.append(et)
    return out


def _parse_event_type(raw: object, path: str, errors: list[str]) -> EventTypeSpec:
    if not isinstance(raw, dict):
        errors.append(f"{path}: must be a mapping")
        return EventTypeSpec(value="", display="", fields=[])
    value = _required_slug(raw.get("value"), f"{path}.value", errors)
    display = _required_str(raw.get("display"), f"{path}.display", errors)

    fields_raw = raw.get("fields")
    fields: list[FieldSpec] = []
    if not isinstance(fields_raw, list) or not fields_raw:
        errors.append(f"{path}.fields: at least one field is required")
    else:
        seen_keys: set[str] = set()
        for j, f_raw in enumerate(fields_raw):
            f = _parse_field(f_raw, f"{path}.fields[{j}]", errors)
            if f.key and f.key in seen_keys:
                errors.append(f"{path}.fields[{j}].key: duplicate key {f.key!r}")
            seen_keys.add(f.key)
            fields.append(f)

    required_raw = raw.get("required") or []
    if not isinstance(required_raw, list):
        errors.append(f"{path}.required: must be a list of field keys")
        required_raw = []
    keys = {f.key for f in fields}
    for r in required_raw:
        if r not in keys:
            errors.append(f"{path}.required: {r!r} is not a declared field key")
    required = [r for r in required_raw if r in keys]

    is_active = raw.get("is_active", True)
    if not isinstance(is_active, bool):
        errors.append(f"{path}.is_active: must be true or false")
        is_active = True
    icon_id = raw.get("icon_id")
    if icon_id is not None and not isinstance(icon_id, str):
        errors.append(f"{path}.icon_id: must be a string")
        icon_id = None
    return EventTypeSpec(
        value=value, display=display, fields=fields,
        required=required, is_active=is_active, icon_id=icon_id,
    )


def _parse_field(raw: object, path: str, errors: list[str]) -> FieldSpec:
    if not isinstance(raw, dict):
        errors.append(f"{path}: must be a mapping")
        return FieldSpec(key="", label="", type="string")
    key = _required_slug(raw.get("key"), f"{path}.key", errors)
    label = _required_str(raw.get("label"), f"{path}.label", errors)
    ftype = raw.get("type")
    if ftype not in SUPPORTED_TYPES:
        supported = ", ".join(sorted(SUPPORTED_TYPES))
        errors.append(f"{path}.type: unsupported type {ftype!r} (supported: {supported})")
        ftype = "string"

    options: list[OptionSpec] | None = None
    if ftype in CHOICE_TYPES:
        options = _parse_options(raw.get("options"), f"{path}.options", errors)
    elif raw.get("options") is not None:
        errors.append(f"{path}.options: not allowed for type {ftype!r}")

    minimum = raw.get("min")
    maximum = raw.get("max")
    for name, val in (("min", minimum), ("max", maximum)):
        if val is not None:
            if ftype not in NUMERIC_TYPES:
                errors.append(f"{path}.{name}: only allowed on integer/number fields")
            elif isinstance(val, bool) or not isinstance(val, (int, float)):
                errors.append(f"{path}.{name}: must be a number")
    if ftype not in NUMERIC_TYPES:
        minimum = maximum = None
    return FieldSpec(key=key, label=label, type=ftype, options=options, min=minimum, max=maximum)


def _parse_options(raw: object, path: str, errors: list[str]) -> list[OptionSpec]:
    if not isinstance(raw, list) or not raw:
        errors.append(f"{path}: required non-empty list for select/multiselect")
        return []
    out: list[OptionSpec] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        opt = _parse_option(item, f"{path}[{i}]", errors)
        if opt is None:
            continue
        if opt.value in seen:
            errors.append(f"{path}[{i}]: duplicate option value {opt.value!r}")
        seen.add(opt.value)
        out.append(opt)
    return out


def _parse_option(item: object, path: str, errors: list[str]) -> OptionSpec | None:
    if isinstance(item, str):
        value, display = item, item.replace("_", " ").title()
    elif isinstance(item, dict):
        value = item.get("value")
        display = item.get("display")
        if not isinstance(value, str) or not value or not isinstance(display, str) or not display:
            errors.append(f"{path}: option mapping needs 'value' and 'display' strings")
            return None
    else:
        errors.append(f"{path}: option must be a string or a value/display mapping")
        return None
    if not _SLUG_RE.match(value):
        errors.append(f"{path}: option value {value!r} must match [a-z0-9_]+")
    return OptionSpec(value=value, display=display)


def _required_str(val: object, path: str, errors: list[str]) -> str:
    if not isinstance(val, str) or not val:
        errors.append(f"{path}: required string")
        return ""
    return val


def _required_slug(val: object, path: str, errors: list[str]) -> str:
    val = _required_str(val, path, errors)
    if val and not _SLUG_RE.match(val):
        errors.append(f"{path}: {val!r} must match [a-z0-9_]+")
    return val
