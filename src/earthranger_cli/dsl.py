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
    "string",
    "textarea",
    "integer",
    "number",
    "boolean",
    "date",
    "datetime",
    "url",
    "select",
    "multiselect",
}
CHOICE_TYPES = {"select", "multiselect"}
# EventType.default_priority vocabulary (das activity/constants.py)
PRIORITY_BY_NAME = {"gray": 0, "green": 100, "amber": 200, "red": 300}
PRIORITY_BY_VALUE = {v: k for k, v in PRIORITY_BY_NAME.items()}
STATE_VALUES = ("new", "active", "resolved")
NUMERIC_TYPES = {"integer", "number"}
_SLUG_RE = re.compile(r"^[a-z0-9_]+$")
# ER's own field-name rule (das FORM_ELEMENT_SEGMENT_PATTERN): stock event
# types use hyphens and uppercase in field keys, so keys are looser than the
# category/event-type value slugs (which appear in URLs).
FIELD_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
# Explicit choices_field names: ER Choice.field is varchar(40); characters kept
# to what survives the $ref query string and ER's stock naming.
CHOICES_FIELD_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class SpecError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


@dataclass
class OptionSpec:
    value: str
    display: str
    icon: str | None = None


@dataclass
class FieldSpec:
    key: str
    label: str
    type: str
    options: list[OptionSpec] | None = None
    min: float | None = None
    max: float | None = None
    hint: str | None = None  # ER's "Hint" -> ui placeholder (max 32 chars)
    description: str | None = None
    default: object = None  # None means "not set" (False/"" are real defaults)
    format: str | None = None  # string fields only: url | email | uuid
    column: str = "left"  # "right" needs layout columns: 2
    choices_field: str | None = None  # explicit Choice.field name (overrides derivation)


# Types whose ER UI variant has a placeholder slot (boolean/date/datetime don't).
HINT_TYPES = {"string", "textarea", "url", "integer", "number", "select", "multiselect"}
# DSL format values for string fields; wire values live in schema_gen.
FORMAT_VALUES = {"url", "email", "uuid"}
# Types whose ER json variant accepts a default, and the Python type it must be.
_DEFAULT_RULES = {
    "string": (str, "a string"),
    "textarea": (str, "a string"),
    "url": (str, "a string"),
    "integer": ((int, float), "a number"),
    "number": ((int, float), "a number"),
    "boolean": (bool, "true or false"),
}


@dataclass
class LayoutSpec:
    label: str = "Details"
    columns: int = 1


@dataclass
class SectionSpec:
    label: str = "Details"
    columns: int = 1
    fields: list[FieldSpec] = field(default_factory=list)


@dataclass
class EventTypeSpec:
    value: str
    display: str
    fields: list[FieldSpec]  # flat view across sections
    required: list[str] = field(default_factory=list)
    is_active: bool = True
    icon_id: str | None = None
    is_collection: bool = False
    default_priority: int | None = None  # wire value; DSL accepts names too
    default_state: str | None = None
    readonly: bool | None = None  # None = never sent; server value preserved
    layout: LayoutSpec = field(default_factory=LayoutSpec)
    sections: list[SectionSpec] | None = None

    def __post_init__(self):
        if self.sections is None:
            # single-section sugar: fields + layout describe one section
            self.sections = [
                SectionSpec(
                    label=self.layout.label, columns=self.layout.columns, fields=self.fields
                )
            ]
        elif not self.fields:
            self.fields = [f for s in self.sections for f in s.fields]


@dataclass
class CategorySpec:
    value: str
    display: str


@dataclass
class Spec:
    category: CategorySpec
    event_types: list[EventTypeSpec]
    # top-level shared choice sets: Choice.field name -> options
    choices: dict[str, list[OptionSpec]] = field(default_factory=dict)


def load_spec(path: str) -> Spec:
    with open(path, encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise SpecError([f"{path}: invalid YAML: {e}"]) from e
    return parse_spec(data)


def parse_spec(data: object) -> Spec:
    errors: list[str] = []
    if not isinstance(data, dict):
        raise SpecError(["spec must be a mapping with 'category' and 'event_types' keys"])
    category = _parse_category(data.get("category"), errors)
    choice_sets = _parse_choice_sets(data.get("choices"), errors)
    event_types = _parse_event_types(data.get("event_types"), errors)
    _check_choice_sets(choice_sets, event_types, errors)
    if errors:
        raise SpecError(errors)
    return Spec(category=category, event_types=event_types, choices=choice_sets)


def _parse_choice_sets(raw: object, errors: list[str]) -> dict[str, list[OptionSpec]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append("choices: must be a mapping of choice-set name -> options list")
        return {}
    out: dict[str, list[OptionSpec]] = {}
    for name, opts_raw in raw.items():
        if not isinstance(name, str) or not CHOICES_FIELD_RE.match(name):
            errors.append(
                f"choices: set name {name!r} must match [A-Za-z0-9_-] and be at most 40 characters"
            )
            continue
        out[name] = _parse_options(opts_raw, f"choices.{name}", errors)
    return out


def _check_choice_sets(
    choice_sets: dict[str, list[OptionSpec]],
    event_types: list[EventTypeSpec],
    errors: list[str],
) -> None:
    """Choice-set coherence across the whole spec.

    Effective names (see schema_gen.effective_choice_field) must be unique
    unless deliberately shared: referencing a top-level set, or explicit
    choices_field declarations with identical inline options. Accidental
    derived-name collisions still error — apply would corrupt the server's
    choice set otherwise.
    """
    from .schema_gen import effective_choice_field  # local import: schema_gen imports from dsl

    # name -> (path, kind: toplevel|explicit|derived, options)
    seen: dict[str, tuple[str, str, list]] = {
        name: (f"choices.{name}", "toplevel", opts) for name, opts in choice_sets.items()
    }
    for i, et in enumerate(event_types):
        for j, f in enumerate(et.fields):
            if f.type not in CHOICE_TYPES or not f.key:
                continue
            name = effective_choice_field(et.value, f)
            path = f"event_types[{i}].fields[{j}]"
            if f.options is None:
                if name not in choice_sets:
                    errors.append(
                        f"{path}.choices_field: {name!r} references an undeclared "
                        "top-level choice set"
                    )
                continue
            first = seen.get(name)
            if first is None:
                kind = "explicit" if f.choices_field else "derived"
                seen[name] = (f"{path}.key", kind, f.options)
                continue
            first_path, kind, first_options = first
            if kind == "toplevel":
                errors.append(
                    f"{path}.options: choice set {name!r} is declared top-level; drop "
                    "the inline options and reference it with choices_field"
                )
            elif kind == "derived" and f.choices_field is None:
                errors.append(
                    f"{path}.key: choice field name {name!r} collides with {first_path} "
                    "(choice-list field names are derived from '<event_type>_<field_key>' "
                    "and must be unique across the spec)"
                )
            elif f.options != first_options:
                errors.append(
                    f"{path}.key: choices_field {name!r} is shared with {first_path} "
                    "but the two fields declare different options; shared choice "
                    "sets must declare identical options"
                )


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

    sections_raw = raw.get("sections")
    sections: list[SectionSpec] | None = None
    fields: list[FieldSpec] = []
    if sections_raw is not None:
        if raw.get("fields") is not None or raw.get("layout") is not None:
            errors.append(f"{path}: 'sections' is mutually exclusive with 'fields' and 'layout'")
        sections = _parse_sections(sections_raw, f"{path}.sections", errors)
        fields = [f for sec in sections for f in sec.fields]
    else:
        fields_raw = raw.get("fields")
        if not isinstance(fields_raw, list):
            errors.append(f"{path}.fields: at least one field is required")
        elif fields_raw:
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
    is_collection = raw.get("is_collection", False)
    if not isinstance(is_collection, bool):
        errors.append(f"{path}.is_collection: must be true or false")
        is_collection = False

    default_priority = raw.get("default_priority")
    if default_priority is not None:
        if isinstance(default_priority, str) and default_priority in PRIORITY_BY_NAME:
            default_priority = PRIORITY_BY_NAME[default_priority]
        elif isinstance(default_priority, bool) or default_priority not in PRIORITY_BY_VALUE:
            names = ", ".join(PRIORITY_BY_NAME)
            values = ", ".join(str(v) for v in sorted(PRIORITY_BY_VALUE))
            errors.append(f"{path}.default_priority: must be one of {names} (or {values})")
            default_priority = None

    default_state = raw.get("default_state")
    if default_state is not None and default_state not in STATE_VALUES:
        errors.append(f"{path}.default_state: must be one of {', '.join(STATE_VALUES)}")
        default_state = None

    readonly = raw.get("readonly")
    if readonly is not None and not isinstance(readonly, bool):
        errors.append(f"{path}.readonly: must be true or false")
        readonly = None
    icon_id = raw.get("icon_id")
    if icon_id is not None and not isinstance(icon_id, str):
        errors.append(f"{path}.icon_id: must be a string")
        icon_id = None
    layout = _parse_layout(raw.get("layout"), f"{path}.layout", errors)
    if sections is None:
        for j, f in enumerate(fields):
            if f.column == "right" and layout.columns != 2:
                errors.append(f"{path}.fields[{j}].column: 'right' requires layout columns: 2")
    return EventTypeSpec(
        value=value,
        display=display,
        fields=fields,
        required=required,
        is_active=is_active,
        icon_id=icon_id,
        is_collection=is_collection,
        default_priority=default_priority,
        default_state=default_state,
        readonly=readonly,
        layout=layout,
        sections=sections,
    )


def _parse_sections(raw: object, path: str, errors: list[str]) -> list[SectionSpec]:
    if not isinstance(raw, list) or not raw:
        errors.append(f"{path}: at least one section is required")
        return []
    sections: list[SectionSpec] = []
    seen_keys: set[str] = set()
    for k, sec_raw in enumerate(raw):
        sec_path = f"{path}[{k}]"
        if not isinstance(sec_raw, dict):
            errors.append(f"{sec_path}: must be a mapping")
            continue
        label = sec_raw.get("label", "Details")
        if not isinstance(label, str):
            errors.append(f"{sec_path}.label: must be a string")
            label = "Details"
        columns = sec_raw.get("columns", 1)
        if columns not in (1, 2):
            errors.append(f"{sec_path}.columns: must be 1 or 2")
            columns = 1
        fields_raw = sec_raw.get("fields")
        sec_fields: list[FieldSpec] = []
        if not isinstance(fields_raw, list) or not fields_raw:
            errors.append(f"{sec_path}.fields: at least one field is required")
        else:
            for j, f_raw in enumerate(fields_raw):
                f = _parse_field(f_raw, f"{sec_path}.fields[{j}]", errors)
                if f.key and f.key in seen_keys:
                    errors.append(f"{sec_path}.fields[{j}].key: duplicate key {f.key!r}")
                seen_keys.add(f.key)
                if f.column == "right" and columns != 2:
                    errors.append(
                        f"{sec_path}.fields[{j}].column: 'right' requires section columns: 2"
                    )
                sec_fields.append(f)
        sections.append(SectionSpec(label=label, columns=columns, fields=sec_fields))
    return sections


def _parse_layout(raw: object, path: str, errors: list[str]) -> LayoutSpec:
    if raw is None:
        return LayoutSpec()
    if not isinstance(raw, dict):
        errors.append(f"{path}: must be a mapping with 'label' and/or 'columns'")
        return LayoutSpec()
    label = raw.get("label", "Details")
    if not isinstance(label, str):
        errors.append(f"{path}.label: must be a string")
        label = "Details"
    columns = raw.get("columns", 1)
    if columns not in (1, 2):
        errors.append(f"{path}.columns: must be 1 or 2")
        columns = 1
    return LayoutSpec(label=label, columns=columns)


def _parse_field(raw: object, path: str, errors: list[str]) -> FieldSpec:
    if not isinstance(raw, dict):
        errors.append(f"{path}: must be a mapping")
        return FieldSpec(key="", label="", type="string")
    key = _required_str(raw.get("key"), f"{path}.key", errors)
    if key and not FIELD_KEY_RE.match(key):
        errors.append(f"{path}.key: {key!r} must match [a-zA-Z0-9_-]+")
    label = _required_str(raw.get("label"), f"{path}.label", errors)
    ftype = raw.get("type")
    if ftype not in SUPPORTED_TYPES:
        supported = ", ".join(sorted(SUPPORTED_TYPES))
        errors.append(f"{path}.type: unsupported type {ftype!r} (supported: {supported})")
        ftype = "string"

    options: list[OptionSpec] | None = None
    if ftype in CHOICE_TYPES:
        if raw.get("options") is None and raw.get("choices_field") is not None:
            options = None  # references a top-level choice set (validated spec-wide)
        else:
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

    hint = raw.get("hint")
    if hint is not None:
        if ftype not in HINT_TYPES:
            errors.append(f"{path}.hint: not allowed for type {ftype!r}")
            hint = None
        elif not isinstance(hint, str):
            errors.append(f"{path}.hint: must be a string")
            hint = None
        elif len(hint) > 32:
            errors.append(f"{path}.hint: must be at most 32 characters")

    description = raw.get("description")
    if description is not None and not isinstance(description, str):
        errors.append(f"{path}.description: must be a string")
        description = None

    default = raw.get("default")
    if default is not None:
        rule = _DEFAULT_RULES.get(ftype)
        if rule is None:
            errors.append(f"{path}.default: not allowed for type {ftype!r}")
            default = None
        else:
            expected, label_ = rule
            wrong_bool = expected is not bool and isinstance(default, bool)
            if wrong_bool or not isinstance(default, expected):
                errors.append(f"{path}.default: must be {label_} for type {ftype!r}")
                default = None

    choices_field = raw.get("choices_field")
    if choices_field is not None:
        if ftype not in CHOICE_TYPES:
            errors.append(f"{path}.choices_field: only allowed on select/multiselect")
            choices_field = None
        elif not isinstance(choices_field, str) or not CHOICES_FIELD_RE.match(choices_field):
            errors.append(
                f"{path}.choices_field: must match [A-Za-z0-9_-] and be at most 40 characters"
            )
            choices_field = None

    column = raw.get("column", "left")
    if column not in ("left", "right"):
        errors.append(f"{path}.column: must be 'left' or 'right'")
        column = "left"

    fmt = raw.get("format")
    if fmt is not None:
        if ftype != "string":
            errors.append(f"{path}.format: only allowed on string fields")
            fmt = None
        elif fmt not in FORMAT_VALUES:
            supported = ", ".join(sorted(FORMAT_VALUES))
            errors.append(f"{path}.format: unsupported format {fmt!r} (supported: {supported})")
            fmt = None

    return FieldSpec(
        key=key,
        label=label,
        type=ftype,
        options=options,
        min=minimum,
        max=maximum,
        hint=hint,
        description=description,
        default=default,
        format=fmt,
        column=column,
        choices_field=choices_field,
    )


def _parse_options(raw: object, path: str, errors: list[str]) -> list[OptionSpec]:
    if not isinstance(raw, list):
        errors.append(f"{path}: required list for select/multiselect")
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
    icon = None
    if isinstance(item, str):
        value, display = item, item.replace("_", " ").title()
    elif isinstance(item, dict):
        value = item.get("value")
        display = item.get("display")
        if not isinstance(value, str) or not value or not isinstance(display, str) or not display:
            errors.append(f"{path}: option mapping needs 'value' and 'display' strings")
            return None
        icon = item.get("icon")
        if icon is not None and not isinstance(icon, str):
            errors.append(f"{path}.icon: must be a string")
            icon = None
    else:
        errors.append(f"{path}: option must be a string or a value/display mapping")
        return None
    # Option values are free text on ER (Choice.value is an unconstrained
    # varchar(100), truncated at generation time); no slug rule here.
    return OptionSpec(value=value, display=display, icon=icon)


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
