# er-events-cli Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A standalone CLI (`er-events`) that creates and edits EarthRanger event categories, choices, and v2 event types from a YAML field-DSL spec, and posts events — authenticating with username/password.

**Architecture:** A Click CLI over small pure modules: `dsl.py` parses/validates the YAML spec into dataclasses, `schema_gen.py` turns fields into the ER v2 `json`/`ui` schema envelope, `choices.py` plans choice-record diffs, `apply.py` orchestrates idempotent upserts (category → choices → event types) via `earthranger-client`, `events.py` builds/posts events. All API access goes through an injected client object so tests use a fake.

**Tech Stack:** Python ≥3.11, `earthranger-client` (PyPI ≥1.16.0), `click`, `pyyaml`, pytest, ruff, uv/hatchling packaging.

**Spec:** `docs/superpowers/specs/2026-08-31-er-events-cli-design.md` (read it first; this plan implements it exactly).

## Global Constraints

- Repo root: `~/padas/er-events-cli`. All paths below are relative to it.
- Python ≥3.11; stdlib `dataclasses` (NO pydantic).
- Runtime deps exactly: `earthranger-client>=1.16.0`, `click>=8.1`, `pyyaml>=6.0`. Dev deps: `pytest>=8.0`, `ruff>=0.4`.
- Console script name: `er-events`; package: `er_events_cli` under `src/`.
- v2 event types ONLY. API version string passed to erclient is always `"v2.0"`.
- ER varchar limit: 100 chars — applied to choice `field`, `value`, `display`.
- Choice model constant: `"activity.event"`. Choices REST path: `"choices"`.
- OAuth client id: `"das_web_client"`.
- Never DELETE anything; deactivation = `is_active: false`.
- Install for testing: `uv pip install --system -e ".[dev]"` (if that errors on an externally-managed environment, use `python3 -m pip install --break-system-packages -e ".[dev]"`). Run tests with bare `pytest` (never `uv run`).
- Every commit message ends with the trailer line: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

## Verified facts about `earthranger-client` (do not re-derive)

- `ERClient(service_root="https://site.pamdas.org", username=..., password=..., client_id=...)` — `token_url` auto-defaults to `{service_root}/oauth2/token`; do not pass it.
- `client.get_event_categories(include_inactive=True)` → list of dicts.
- `client.get_event_types(include_inactive=True, include_schema=True, version="v2.0")` → list of dicts.
- `client.post_event_type(event_type_dict, version="v2.0")`, `client.patch_event_type(event_type_dict, version="v2.0")` — v2 PATCH path uses `event_type_dict["value"]` (slug); the dict should also carry `id` from the fetched record.
- `client.post_event_category(dict)`, `client.patch_event_category(dict)` — patch requires `dict["id"]`.
- `client.post_event(dict)` → posts to `activity/events`.
- Generic: `client._get(path, params=..., max_retries=...)`, `client._post(path, payload=...)`, `client._patch(path, payload=...)`. `_get` unwraps the `data` key of the response.
- Choices listing returns a paged dict `{"results": [...], "next": <url or None>}`; follow `next` by calling `client._get(next_url, max_retries=0)`.
- ER's choices endpoint 400s with `"... is not one of the available choices"` when filtering on a `field` value that doesn't exist in the DB yet — treat as "no records".
- Exceptions: `from erclient.er_errors import ERClientException` (subclasses: `ERClientNotFound`, `ERClientBadCredentials`, etc.).
- ER's GET of a v2 event type returns `additionalProperties` in `schema.json` where POST/PATCH require `unevaluatedProperties` — normalize before comparing.

## File Structure

```
pyproject.toml            # packaging, deps, console script, ruff/pytest config
.gitignore
src/er_events_cli/
    __init__.py           # version marker only
    dsl.py                # YAML/JSON spec -> dataclasses + validation (pure)
    schema_gen.py         # FieldSpec -> v2 json/ui envelope (pure)
    client.py             # ERClient construction + choices path helpers
    choices.py            # desired choice records + diff planning (pure)
    apply.py              # orchestration: category -> choices -> event types
    events.py             # event payload building + batch posting
    cli.py                # Click wiring only
tests/
    conftest.py           # FakeER test double
    test_dsl.py
    test_schema_gen.py
    test_client.py
    test_choices.py
    test_apply.py
    test_events.py
    test_cli.py
examples/
    wildlife_monitoring.yaml
    events.yaml
README.md
```

---

### Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/er_events_cli/__init__.py`, `tests/test_package.py`

**Interfaces:**
- Produces: installable package `er_events_cli`; `pytest` runs from repo root.

- [ ] **Step 1: Write files**

`pyproject.toml`:

```toml
[project]
name = "er-events-cli"
version = "0.1.0"
description = "CLI for creating and editing EarthRanger event categories, choices, and v2 event types, and posting events."
requires-python = ">=3.11"
dependencies = [
    "earthranger-client>=1.16.0",
    "click>=8.1",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "ruff>=0.4"]

[project.scripts]
er-events = "er_events_cli.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/er_events_cli"]

[tool.ruff]
line-length = 100
src = ["src", "tests"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`.gitignore`:

```
__pycache__/
*.egg-info/
.pytest_cache/
.ruff_cache/
dist/
.venv/
```

`src/er_events_cli/__init__.py`:

```python
"""er-events-cli: manage EarthRanger event categories, choices, and v2 event types."""

__version__ = "0.1.0"
```

`tests/test_package.py`:

```python
import er_events_cli


def test_package_importable():
    assert er_events_cli.__version__
```

- [ ] **Step 2: Install and run the test**

Run: `cd ~/padas/er-events-cli && uv pip install --system -e ".[dev]"` then `pytest -v`
Expected: 1 test PASSES. (If install errors about an externally-managed environment: `python3 -m pip install --break-system-packages -e ".[dev]"`.)

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml .gitignore src tests
git commit -m "feat: project scaffolding for er-events-cli

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: DSL parsing and validation (`dsl.py`)

**Files:**
- Create: `src/er_events_cli/dsl.py`
- Test: `tests/test_dsl.py`

**Interfaces:**
- Produces (used by every later task):
  - `SUPPORTED_TYPES: set[str]`, `CHOICE_TYPES: set[str]` (= `{"select", "multiselect"}`)
  - `@dataclass OptionSpec(value: str, display: str)`
  - `@dataclass FieldSpec(key: str, label: str, type: str, options: list[OptionSpec] | None = None, min: float | None = None, max: float | None = None)`
  - `@dataclass EventTypeSpec(value: str, display: str, fields: list[FieldSpec], required: list[str] = [], is_active: bool = True, icon_id: str | None = None)`
  - `@dataclass CategorySpec(value: str, display: str)`
  - `@dataclass Spec(category: CategorySpec, event_types: list[EventTypeSpec])`
  - `class SpecError(Exception)` with `.errors: list[str]`
  - `parse_spec(data: object) -> Spec` (raises `SpecError` with ALL problems collected)
  - `load_spec(path: str) -> Spec` (reads file, `yaml.safe_load`, then `parse_spec`)

- [ ] **Step 1: Write failing happy-path tests**

`tests/test_dsl.py`:

```python
import json

import pytest

from er_events_cli.dsl import OptionSpec, SpecError, load_spec, parse_spec

VALID = {
    "category": {"value": "wildlife_monitoring", "display": "Wildlife Monitoring"},
    "event_types": [
        {
            "value": "animal_sighting",
            "display": "Animal Sighting",
            "icon_id": "mammal_rep",
            "fields": [
                {
                    "key": "species",
                    "label": "Species",
                    "type": "select",
                    "options": [{"value": "elephant", "display": "Elephant"}, "spotted_lion"],
                },
                {"key": "count", "label": "Number of animals", "type": "integer", "min": 0},
                {"key": "notes", "label": "Notes", "type": "textarea"},
            ],
            "required": ["species"],
        }
    ],
}


def test_parse_valid_spec():
    spec = parse_spec(VALID)
    assert spec.category.value == "wildlife_monitoring"
    et = spec.event_types[0]
    assert et.value == "animal_sighting"
    assert et.icon_id == "mammal_rep"
    assert et.is_active is True
    assert et.required == ["species"]
    assert [f.key for f in et.fields] == ["species", "count", "notes"]
    assert et.fields[1].min == 0
    assert et.fields[1].max is None


def test_option_shorthand_derives_value_and_display():
    spec = parse_spec(VALID)
    options = spec.event_types[0].fields[0].options
    assert options[1] == OptionSpec(value="spotted_lion", display="Spotted Lion")


def test_json_file_is_accepted(tmp_path):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(VALID))
    spec = load_spec(str(p))
    assert spec.category.value == "wildlife_monitoring"


def test_yaml_file_loads(tmp_path):
    p = tmp_path / "spec.yaml"
    p.write_text(
        "category: {value: c1, display: C1}\n"
        "event_types:\n"
        "  - value: t1\n"
        "    display: T1\n"
        "    fields:\n"
        "      - {key: notes, label: Notes, type: string}\n"
    )
    spec = load_spec(str(p))
    assert spec.event_types[0].fields[0].type == "string"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_dsl.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'er_events_cli.dsl'`

- [ ] **Step 3: Implement `dsl.py`**

Write the complete module (parsing AND validation together — validation tests come in Step 5 and must pass against this same code):

```python
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
```

- [ ] **Step 4: Run happy-path tests**

Run: `pytest tests/test_dsl.py -v`
Expected: all 4 PASS

- [ ] **Step 5: Write the validation tests and confirm they pass (Step 3's code already validates — these verify each message)**

Append to `tests/test_dsl.py`:

```python
def _errors_for(data):
    with pytest.raises(SpecError) as exc:
        parse_spec(data)
    return exc.value.errors


def _spec_with_field(field):
    return {
        "category": {"value": "c1", "display": "C1"},
        "event_types": [
            {"value": "t1", "display": "T1", "fields": [field]},
        ],
    }


def test_missing_category_and_event_types_collected_together():
    errors = _errors_for({})
    assert "category: required mapping with 'value' and 'display'" in errors
    assert "event_types: at least one event type is required" in errors


def test_bad_slug_rejected():
    errors = _errors_for(
        {
            "category": {"value": "Wildlife Monitoring", "display": "X"},
            "event_types": [{"value": "t1", "display": "T1",
                             "fields": [{"key": "notes", "label": "N", "type": "string"}]}],
        }
    )
    assert "category.value: 'Wildlife Monitoring' must match [a-z0-9_]+" in errors


def test_unknown_type_rejected():
    errors = _errors_for(_spec_with_field({"key": "f1", "label": "F", "type": "selects"}))
    assert any(e.startswith("event_types[0].fields[0].type: unsupported type 'selects'") for e in errors)


def test_options_required_for_select_and_forbidden_otherwise():
    errors = _errors_for(_spec_with_field({"key": "f1", "label": "F", "type": "select"}))
    assert "event_types[0].fields[0].options: required non-empty list for select/multiselect" in errors
    errors = _errors_for(
        _spec_with_field({"key": "f1", "label": "F", "type": "string", "options": ["a"]})
    )
    assert "event_types[0].fields[0].options: not allowed for type 'string'" in errors


def test_min_only_on_numeric():
    errors = _errors_for(_spec_with_field({"key": "f1", "label": "F", "type": "string", "min": 0}))
    assert "event_types[0].fields[0].min: only allowed on integer/number fields" in errors


def test_required_must_name_declared_field():
    data = _spec_with_field({"key": "f1", "label": "F", "type": "string"})
    data["event_types"][0]["required"] = ["nope"]
    errors = _errors_for(data)
    assert "event_types[0].required: 'nope' is not a declared field key" in errors


def test_duplicates_rejected():
    data = {
        "category": {"value": "c1", "display": "C1"},
        "event_types": [
            {"value": "t1", "display": "T1", "fields": [
                {"key": "f1", "label": "F", "type": "string"},
                {"key": "f1", "label": "F2", "type": "string"},
                {"key": "f2", "label": "F3", "type": "select", "options": ["a", "a"]},
            ]},
            {"value": "t1", "display": "T1 again", "fields": [
                {"key": "g1", "label": "G", "type": "string"}]},
        ],
    }
    errors = _errors_for(data)
    assert "event_types[0].fields[1].key: duplicate key 'f1'" in errors
    assert "event_types[0].fields[2].options[1]: duplicate option value 'a'" in errors
    assert "event_types[1].value: duplicate event type value 't1'" in errors
```

Run: `pytest tests/test_dsl.py -v`
Expected: all PASS (if any message mismatches, fix the TEST to match the implementation only when the implementation matches the spec's intent; otherwise fix the implementation).

- [ ] **Step 6: Commit**

```bash
git add src/er_events_cli/dsl.py tests/test_dsl.py
git commit -m "feat: spec DSL parsing and validation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Schema generation — scalar fields (`schema_gen.py`)

**Files:**
- Create: `src/er_events_cli/schema_gen.py`
- Test: `tests/test_schema_gen.py`

**Interfaces:**
- Consumes: `FieldSpec`, `EventTypeSpec` from `er_events_cli.dsl`.
- Produces:
  - `VARCHAR_LIMIT = 100`, `SECTION_ID = "section-1"`
  - `choice_field_name(event_type_value: str, field_key: str) -> str`
  - `build_property_pair(field: FieldSpec, event_type_value: str) -> tuple[dict, dict]` — `(json_property, ui_field)`
  - (Task 4 adds `build_schema`, `build_event_type_payload`)

- [ ] **Step 1: Write failing tests**

`tests/test_schema_gen.py`:

```python
from er_events_cli.dsl import FieldSpec
from er_events_cli.schema_gen import build_property_pair, choice_field_name


def test_choice_field_name_joins_and_truncates_to_100():
    assert choice_field_name("animal_sighting", "species") == "animal_sighting_species"
    long = choice_field_name("a" * 80, "b" * 80)
    assert len(long) == 100
    assert long == "a" * 80 + "_" + "b" * 19


def test_string_field():
    json_prop, ui = build_property_pair(FieldSpec(key="n", label="Notes", type="string"), "t1")
    assert json_prop == {"type": "string", "title": "Notes"}
    assert ui == {"type": "TEXT", "inputType": "SHORT_TEXT", "parent": "section-1"}


def test_textarea_field():
    _, ui = build_property_pair(FieldSpec(key="n", label="Notes", type="textarea"), "t1")
    assert ui == {"type": "TEXT", "inputType": "LONG_TEXT", "parent": "section-1"}


def test_integer_with_min_max():
    json_prop, ui = build_property_pair(
        FieldSpec(key="c", label="Count", type="integer", min=0, max=500), "t1"
    )
    assert json_prop == {"type": "integer", "title": "Count", "minimum": 0, "maximum": 500}
    assert ui == {"type": "NUMERIC", "parent": "section-1"}


def test_number_boolean_date_datetime():
    json_prop, _ = build_property_pair(FieldSpec(key="r", label="Ratio", type="number"), "t1")
    assert json_prop == {"type": "number", "title": "Ratio"}
    json_prop, ui = build_property_pair(FieldSpec(key="i", label="Injured", type="boolean"), "t1")
    assert json_prop == {"type": "boolean", "title": "Injured"}
    assert ui == {"type": "BOOLEAN", "parent": "section-1"}
    json_prop, ui = build_property_pair(FieldSpec(key="d", label="Seen on", type="date"), "t1")
    assert json_prop == {"type": "string", "format": "date", "title": "Seen on"}
    assert ui == {"type": "DATE_TIME", "parent": "section-1"}
    json_prop, _ = build_property_pair(FieldSpec(key="dt", label="At", type="datetime"), "t1")
    assert json_prop == {"type": "string", "format": "date-time", "title": "At"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_schema_gen.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement the scalar half of `schema_gen.py`**

```python
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
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_schema_gen.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/er_events_cli/schema_gen.py tests/test_schema_gen.py
git commit -m "feat: v2 schema generation for scalar field types

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Schema generation — choice fields and the v2 envelope

**Files:**
- Modify: `src/er_events_cli/schema_gen.py`
- Test: `tests/test_schema_gen.py`

**Interfaces:**
- Produces:
  - `build_schema(et: EventTypeSpec) -> dict` — the `{"json": ..., "ui": ...}` envelope
  - `build_event_type_payload(et: EventTypeSpec, category_value: str) -> dict` — full POST/PATCH body

- [ ] **Step 1: Write failing tests**

Append to `tests/test_schema_gen.py`:

```python
from er_events_cli.dsl import EventTypeSpec, OptionSpec
from er_events_cli.schema_gen import build_event_type_payload, build_schema

REF = "/api/v2.0/schemas/choices.json?field=t1_species"


def _select_field(type_="select"):
    return FieldSpec(
        key="species", label="Species", type=type_,
        options=[OptionSpec("elephant", "Elephant")],
    )


def test_select_field():
    json_prop, ui = build_property_pair(_select_field(), "t1")
    assert json_prop == {"type": "string", "title": "Species", "anyOf": [{"$ref": REF}]}
    assert ui == {
        "type": "CHOICE_LIST",
        "inputType": "DROPDOWN",
        "placeholder": "",
        "choices": {
            "type": "EXISTING_CHOICE_LIST",
            "existingChoiceList": ["t1_species"],
            "eventTypeCategories": [],
            "featureCategories": [],
            "myDataType": "",
            "subjectGroups": [],
            "subjectSubtypes": [],
        },
        "parent": "section-1",
    }


def test_multiselect_field():
    json_prop, _ = build_property_pair(_select_field("multiselect"), "t1")
    assert json_prop == {
        "type": "array",
        "title": "Species",
        "uniqueItems": True,
        "items": {"type": "string", "anyOf": [{"$ref": REF}]},
    }


def _event_type():
    return EventTypeSpec(
        value="t1", display="T One",
        fields=[_select_field(), FieldSpec(key="notes", label="Notes", type="string")],
        required=["species"],
    )


def test_build_schema_envelope():
    schema = build_schema(_event_type())
    assert schema["json"]["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["json"]["type"] == "object"
    assert schema["json"]["unevaluatedProperties"] is False
    assert schema["json"]["required"] == ["species"]
    assert list(schema["json"]["properties"]) == ["species", "notes"]
    section = schema["ui"]["sections"]["section-1"]
    assert section["label"] == "Details"
    assert section["leftColumn"] == [
        {"name": "species", "type": "field"},
        {"name": "notes", "type": "field"},
    ]
    assert schema["ui"]["order"] == ["section-1"]
    assert schema["ui"]["headers"] == {}
    assert list(schema["ui"]["fields"]) == ["species", "notes"]


def test_build_event_type_payload():
    payload = build_event_type_payload(_event_type(), "wildlife_monitoring")
    assert payload["value"] == "t1"
    assert payload["display"] == "T One"
    assert payload["category"] == "wildlife_monitoring"
    assert payload["is_active"] is True
    assert payload["readonly"] is False
    assert payload["schema"] == build_schema(_event_type())
    assert "icon_id" not in payload
    et = _event_type()
    et.icon_id = "mammal_rep"
    assert build_event_type_payload(et, "c")["icon_id"] == "mammal_rep"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_schema_gen.py -v`
Expected: new tests FAIL (`NotImplementedError` / missing names)

- [ ] **Step 3: Implement**

Replace `_build_choice_pair`'s `raise NotImplementedError` body and append the two builders:

```python
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
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_schema_gen.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/er_events_cli/schema_gen.py tests/test_schema_gen.py
git commit -m "feat: choice fields and v2 event-type envelope generation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Client construction and choices helpers (`client.py`)

**Files:**
- Create: `src/er_events_cli/client.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: `erclient.client.ERClient`, `erclient.er_errors.ERClientException`.
- Produces:
  - `DEFAULT_CLIENT_ID = "das_web_client"`, `CHOICE_MODEL = "activity.event"`, `CHOICES_PATH = "choices"`
  - `normalize_server(server: str) -> str`
  - `make_client(*, server: str, username: str, password: str) -> ERClient`
  - `get_choices(client, field_name: str) -> list[dict]` (paged; 400-on-unknown-field → `[]`)
  - `post_choice(client, payload: dict) -> dict`
  - `patch_choice(client, choice_id: str, payload: dict) -> dict`

- [ ] **Step 1: Write failing tests**

`tests/test_client.py`:

```python
from unittest.mock import Mock

import pytest
from erclient.er_errors import ERClientException

from er_events_cli.client import get_choices, make_client, normalize_server, patch_choice, post_choice


def test_normalize_server_bare_name():
    assert normalize_server("myreserve") == "https://myreserve.pamdas.org"


def test_normalize_server_full_url_and_trailing_slash():
    assert normalize_server("https://x.example.org/") == "https://x.example.org"


def test_make_client_wires_credentials():
    client = make_client(server="myreserve", username="u", password="p")
    assert client.service_root == "https://myreserve.pamdas.org"
    assert client.username == "u"
    assert client.password == "p"
    assert client.client_id == "das_web_client"
    assert client.token_url == "https://myreserve.pamdas.org/oauth2/token"


def test_get_choices_pages_through_results():
    client = Mock()
    client._get.side_effect = [
        {"results": [{"value": "a"}], "next": "https://x/choices?page=2"},
        {"results": [{"value": "b"}], "next": None},
    ]
    result = get_choices(client, "t1_species")
    assert [c["value"] for c in result] == ["a", "b"]
    first_call = client._get.call_args_list[0]
    assert first_call.args[0] == "choices"
    assert first_call.kwargs["params"] == {
        "model": "activity.event",
        "field": "t1_species",
        "include_inactive": True,
        "page_size": 200,
    }
    assert client._get.call_args_list[1].args[0] == "https://x/choices?page=2"


def test_get_choices_unknown_field_400_means_empty():
    client = Mock()
    client._get.side_effect = ERClientException(
        "Failed to call ER web service. 'field': 't1_species' is not one of the available choices."
    )
    assert get_choices(client, "t1_species") == []


def test_get_choices_other_errors_propagate():
    client = Mock()
    client._get.side_effect = ERClientException("500 server error")
    with pytest.raises(ERClientException):
        get_choices(client, "t1_species")


def test_post_and_patch_choice_paths():
    client = Mock()
    post_choice(client, {"value": "a"})
    client._post.assert_called_once_with("choices", payload={"value": "a"})
    patch_choice(client, "abc123", {"is_active": False})
    client._patch.assert_called_once_with("choices/abc123", payload={"is_active": False})
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_client.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `client.py`**

```python
"""ERClient construction and choices-endpoint helpers.

erclient has no first-class choices methods; we use its generic path methods
(_get/_post/_patch), the same pattern er-smart-sync uses in production.
"""

from __future__ import annotations

from erclient.client import ERClient
from erclient.er_errors import ERClientException

DEFAULT_CLIENT_ID = "das_web_client"
CHOICES_PATH = "choices"
CHOICE_MODEL = "activity.event"


def normalize_server(server: str) -> str:
    server = server.strip().rstrip("/")
    if not server.startswith(("http://", "https://")):
        server = f"https://{server}.pamdas.org"
    return server


def make_client(*, server: str, username: str, password: str) -> ERClient:
    return ERClient(
        service_root=normalize_server(server),
        username=username,
        password=password,
        client_id=DEFAULT_CLIENT_ID,
    )


def get_choices(client, field_name: str) -> list[dict]:
    """All Choice records for (model=activity.event, field=field_name), inactive included.

    ER validates the field= filter against field values already in the DB and
    400s for a never-seen name; that means "no records for this field yet".
    """
    try:
        page = client._get(
            CHOICES_PATH,
            params={
                "model": CHOICE_MODEL,
                "field": field_name,
                "include_inactive": True,
                "page_size": 200,
            },
            max_retries=0,
        )
    except ERClientException as e:
        if "is not one of the available choices" in str(e):
            return []
        raise

    results: list[dict] = []
    while True:
        if isinstance(page, dict) and "results" in page:
            results.extend(page["results"])
            next_url = page.get("next")
            if not next_url:
                break
            page = client._get(next_url, max_retries=0)
        elif isinstance(page, list):
            results.extend(page)
            break
        else:
            break
    return results


def post_choice(client, payload: dict) -> dict:
    return client._post(CHOICES_PATH, payload=payload)


def patch_choice(client, choice_id: str, payload: dict) -> dict:
    return client._patch(f"{CHOICES_PATH}/{choice_id}", payload=payload)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_client.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/er_events_cli/client.py tests/test_client.py
git commit -m "feat: ERClient construction and choices endpoint helpers

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Choice planning (`choices.py`)

**Files:**
- Create: `src/er_events_cli/choices.py`
- Test: `tests/test_choices.py`

**Interfaces:**
- Consumes: `EventTypeSpec`, `OptionSpec` from `dsl`; `choice_field_name`, `VARCHAR_LIMIT` from `schema_gen`; `CHOICE_MODEL` from `client`.
- Produces:
  - `@dataclass ChoiceOp(action: str, value: str, payload: dict | None = None, choice_id: str | None = None)` — `action` ∈ `{"create", "update", "deactivate", "unchanged"}`
  - `desired_choice_records(et: EventTypeSpec) -> dict[str, list[dict]]` — choice-field name → desired records
  - `plan_field_choices(existing: list[dict], desired: list[dict]) -> list[ChoiceOp]` (pure)

- [ ] **Step 1: Write failing tests**

`tests/test_choices.py`:

```python
from er_events_cli.choices import ChoiceOp, desired_choice_records, plan_field_choices
from er_events_cli.dsl import EventTypeSpec, FieldSpec, OptionSpec


def _et():
    return EventTypeSpec(
        value="t1", display="T1",
        fields=[
            FieldSpec(key="species", label="Species", type="select",
                      options=[OptionSpec("elephant", "Elephant"), OptionSpec("lion", "Lion")]),
            FieldSpec(key="notes", label="Notes", type="string"),
        ],
    )


def test_desired_choice_records_only_for_choice_fields():
    records = desired_choice_records(_et())
    assert list(records) == ["t1_species"]
    assert records["t1_species"][0] == {
        "model": "activity.event",
        "field": "t1_species",
        "value": "elephant",
        "display": "Elephant",
        "is_active": True,
    }


def test_desired_choice_records_truncate_to_100():
    et = EventTypeSpec(
        value="t", display="T",
        fields=[FieldSpec(key="f", label="F", type="select",
                          options=[OptionSpec("v" * 150, "d" * 150)])],
    )
    rec = desired_choice_records(et)["t_f"][0]
    assert len(rec["value"]) == 100
    assert len(rec["display"]) == 100


DESIRED = [
    {"model": "activity.event", "field": "t1_species", "value": "elephant",
     "display": "Elephant", "is_active": True},
    {"model": "activity.event", "field": "t1_species", "value": "lion",
     "display": "Lion", "is_active": True},
]


def test_plan_create_when_missing():
    ops = plan_field_choices([], DESIRED)
    assert [op.action for op in ops] == ["create", "create"]
    assert ops[0].payload == DESIRED[0]


def test_plan_unchanged_when_identical():
    existing = [
        {"id": "1", "value": "elephant", "display": "Elephant", "is_active": True},
        {"id": "2", "value": "lion", "display": "Lion", "is_active": True},
    ]
    ops = plan_field_choices(existing, DESIRED)
    assert [op.action for op in ops] == ["unchanged", "unchanged"]


def test_plan_update_on_display_change_and_reactivation():
    existing = [
        {"id": "1", "value": "elephant", "display": "Old Name", "is_active": True},
        {"id": "2", "value": "lion", "display": "Lion", "is_active": False},
    ]
    ops = plan_field_choices(existing, DESIRED)
    assert ops[0] == ChoiceOp(action="update", value="elephant",
                              payload={"display": "Elephant", "is_active": True}, choice_id="1")
    assert ops[1].action == "update"
    assert ops[1].payload == {"display": "Lion", "is_active": True}


def test_plan_deactivates_options_removed_from_spec():
    existing = [
        {"id": "1", "value": "elephant", "display": "Elephant", "is_active": True},
        {"id": "2", "value": "lion", "display": "Lion", "is_active": True},
        {"id": "3", "value": "rhino", "display": "Rhino", "is_active": True},
    ]
    ops = plan_field_choices(existing, DESIRED)
    assert ops[-1] == ChoiceOp(action="deactivate", value="rhino",
                               payload={"is_active": False}, choice_id="3")


def test_plan_leaves_already_inactive_removed_options_alone():
    existing = [{"id": "3", "value": "rhino", "display": "Rhino", "is_active": False}]
    ops = plan_field_choices(existing, DESIRED)
    assert [op.action for op in ops] == ["create", "create"]
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_choices.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `choices.py`**

```python
"""Choice-record planning: desired records from a spec, diff against existing. Pure."""

from __future__ import annotations

from dataclasses import dataclass

from .client import CHOICE_MODEL
from .dsl import EventTypeSpec
from .schema_gen import VARCHAR_LIMIT, choice_field_name


@dataclass
class ChoiceOp:
    action: str  # "create" | "update" | "deactivate" | "unchanged"
    value: str
    payload: dict | None = None  # POST/PATCH body; None for unchanged
    choice_id: str | None = None  # set for update/deactivate


def desired_choice_records(et: EventTypeSpec) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in et.fields:
        if f.options is None:
            continue
        name = choice_field_name(et.value, f.key)
        out[name] = [
            {
                "model": CHOICE_MODEL,
                "field": name,
                "value": o.value[:VARCHAR_LIMIT],
                "display": o.display[:VARCHAR_LIMIT],
                "is_active": True,
            }
            for o in f.options
        ]
    return out


def plan_field_choices(existing: list[dict], desired: list[dict]) -> list[ChoiceOp]:
    ops: list[ChoiceOp] = []
    existing_by_value = {c["value"]: c for c in existing}
    desired_values = {d["value"] for d in desired}
    for want in desired:
        have = existing_by_value.get(want["value"])
        if have is None:
            ops.append(ChoiceOp(action="create", value=want["value"], payload=want))
        elif have.get("display") != want["display"] or not have.get("is_active", True):
            ops.append(
                ChoiceOp(
                    action="update",
                    value=want["value"],
                    payload={"display": want["display"], "is_active": True},
                    choice_id=have["id"],
                )
            )
        else:
            ops.append(ChoiceOp(action="unchanged", value=want["value"]))
    for have in existing:
        if have["value"] not in desired_values and have.get("is_active", True):
            ops.append(
                ChoiceOp(
                    action="deactivate",
                    value=have["value"],
                    payload={"is_active": False},
                    choice_id=have["id"],
                )
            )
    return ops
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_choices.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/er_events_cli/choices.py tests/test_choices.py
git commit -m "feat: choice-record diff planning

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Apply orchestration (`apply.py`)

**Files:**
- Create: `src/er_events_cli/apply.py`, `tests/conftest.py`
- Test: `tests/test_apply.py`

**Interfaces:**
- Consumes: `Spec` from `dsl`; `build_event_type_payload` from `schema_gen`; `desired_choice_records`, `plan_field_choices` from `choices`; `get_choices`, `post_choice`, `patch_choice` from `client`.
- Produces:
  - `@dataclass ActionRecord(kind: str, name: str, action: str, detail: str = "")` — `kind` ∈ `{"category", "choice", "event_type"}`, `action` ∈ `{"created", "updated", "unchanged", "deactivated"}`
  - `normalize_v2_schema(schema: dict) -> dict`
  - `extract_choice_fields(schema: dict) -> list[str]` (also used by Task 10's `show event-type`)
  - `class ApplyError(Exception)` — an API write failed; message names the failing object
  - `apply_spec(client, spec: Spec, dry_run: bool = False) -> list[ActionRecord]` (raises `ApplyError`)
  - Test double `FakeER` in `tests/conftest.py` (fixture name `fake_er` NOT used; tests instantiate `FakeER(...)` directly via `from conftest import FakeER` — pytest puts conftest on the path).

- [ ] **Step 1: Write the FakeER test double**

`tests/conftest.py`:

```python
"""FakeER: an in-memory stand-in for erclient.ERClient covering the calls we make."""


class FakeER:
    def __init__(self, categories=None, event_types=None, choices=None):
        self.categories = categories or []
        self.event_types = event_types or []
        self.choices = choices or {}  # field name -> list[dict]
        self.calls = []  # (method, ...) tuples, appended for every call

    # --- categories ---
    def get_event_categories(self, include_inactive=False):
        self.calls.append(("get_event_categories",))
        return self.categories

    def post_event_category(self, data):
        self.calls.append(("post_event_category", data))
        return data

    def patch_event_category(self, data):
        self.calls.append(("patch_event_category", data))
        return data

    # --- event types ---
    def get_event_types(self, include_inactive=False, include_schema=False, version="v1.0"):
        self.calls.append(("get_event_types", version))
        return self.event_types

    def post_event_type(self, event_type, version="v1.0"):
        self.calls.append(("post_event_type", event_type, version))
        return event_type

    def patch_event_type(self, event_type, version="v1.0"):
        self.calls.append(("patch_event_type", event_type, version))
        return event_type

    # --- generic path methods (choices) ---
    def _get(self, path, params=None, max_retries=5, **kwargs):
        self.calls.append(("_get", path, params))
        field = (params or {}).get("field")
        return {"results": list(self.choices.get(field, [])), "next": None}

    def _post(self, path, payload, **kwargs):
        self.calls.append(("_post", path, payload))
        return payload

    def _patch(self, path, payload, **kwargs):
        self.calls.append(("_patch", path, payload))
        return payload

    # --- events ---
    def post_event(self, event):
        self.calls.append(("post_event", event))
        return event

    # helpers for assertions
    def writes(self):
        return [c for c in self.calls if c[0] in (
            "post_event_category", "patch_event_category",
            "post_event_type", "patch_event_type", "_post", "_patch", "post_event",
        )]
```

- [ ] **Step 2: Write failing tests**

`tests/test_apply.py`:

```python
import copy

from conftest import FakeER

from er_events_cli.apply import apply_spec, extract_choice_fields, normalize_v2_schema
from er_events_cli.dsl import parse_spec
from er_events_cli.schema_gen import build_event_type_payload

SPEC_DATA = {
    "category": {"value": "wm", "display": "Wildlife Monitoring"},
    "event_types": [
        {
            "value": "sighting",
            "display": "Sighting",
            "fields": [
                {"key": "species", "label": "Species", "type": "select",
                 "options": [{"value": "elephant", "display": "Elephant"}]},
                {"key": "notes", "label": "Notes", "type": "string"},
            ],
        }
    ],
}


def _spec():
    return parse_spec(copy.deepcopy(SPEC_DATA))


def _existing_state():
    """Server state exactly matching the spec (as ER's GET would return it)."""
    spec = _spec()
    payload = build_event_type_payload(spec.event_types[0], "wm")
    existing_type = copy.deepcopy(payload)
    existing_type["id"] = "et-1"
    # simulate ER's GET quirk: additionalProperties instead of unevaluatedProperties
    del existing_type["schema"]["json"]["unevaluatedProperties"]
    existing_type["schema"]["json"]["additionalProperties"] = False
    # simulate ER returning the category as a nested dict
    existing_type["category"] = {"value": "wm", "display": "Wildlife Monitoring"}
    return FakeER(
        categories=[{"id": "cat-1", "value": "wm", "display": "Wildlife Monitoring"}],
        event_types=[existing_type],
        choices={
            "sighting_species": [
                {"id": "ch-1", "model": "activity.event", "field": "sighting_species",
                 "value": "elephant", "display": "Elephant", "is_active": True},
            ]
        },
    )


def test_normalize_v2_schema_repairs_get_response():
    schema = {"json": {"additionalProperties": False, "properties": {}}}
    fixed = normalize_v2_schema(schema)
    assert fixed["json"]["unevaluatedProperties"] is False
    assert "additionalProperties" not in fixed["json"]


def test_extract_choice_fields():
    schema = {
        "json": {"properties": {
            "a": {"anyOf": [{"$ref": "/api/v2.0/schemas/choices.json?field=t_a"}]},
            "b": {"items": {"anyOf": [{"$ref": "/api/v2.0/schemas/choices.json?field=t_b"}]}},
            "c": {"type": "string"},
        }}
    }
    assert extract_choice_fields(schema) == ["t_a", "t_b"]


def test_fresh_site_creates_everything():
    fake = FakeER()
    records = apply_spec(fake, _spec())
    by = {(r.kind, r.name): r.action for r in records}
    assert by[("category", "wm")] == "created"
    assert by[("choice", "sighting_species:elephant")] == "created"
    assert by[("event_type", "sighting")] == "created"
    methods = [c[0] for c in fake.writes()]
    assert "post_event_category" in methods
    assert "_post" in methods
    assert "post_event_type" in methods
    posted_type = next(c for c in fake.calls if c[0] == "post_event_type")
    assert posted_type[2] == "v2.0"


def test_matching_site_is_all_unchanged():
    fake = _existing_state()
    records = apply_spec(fake, _spec())
    assert {r.action for r in records} == {"unchanged"}
    assert fake.writes() == []


def test_display_change_patches_event_type_with_id():
    fake = _existing_state()
    spec = _spec()
    spec.event_types[0].display = "Animal Sighting"
    records = apply_spec(fake, spec)
    assert ("event_type", "sighting") in {(r.kind, r.name) for r in records if r.action == "updated"}
    patched = next(c for c in fake.calls if c[0] == "patch_event_type")
    assert patched[1]["id"] == "et-1"
    assert patched[1]["display"] == "Animal Sighting"
    assert patched[2] == "v2.0"


def test_category_display_change_patches_category():
    fake = _existing_state()
    spec = _spec()
    spec.category.display = "Wildlife"
    apply_spec(fake, spec)
    patched = next(c for c in fake.calls if c[0] == "patch_event_category")
    assert patched[1] == {"id": "cat-1", "value": "wm", "display": "Wildlife"}


def test_removed_option_is_deactivated():
    fake = _existing_state()
    fake.choices["sighting_species"].append(
        {"id": "ch-2", "model": "activity.event", "field": "sighting_species",
         "value": "rhino", "display": "Rhino", "is_active": True}
    )
    records = apply_spec(fake, _spec())
    assert any(r.action == "deactivated" and r.name == "sighting_species:rhino" for r in records)
    patched = next(c for c in fake.calls if c[0] == "_patch")
    assert patched[1] == "choices/ch-2"
    assert patched[2] == {"is_active": False}


def test_dry_run_performs_no_writes():
    fake = FakeER()
    records = apply_spec(fake, _spec(), dry_run=True)
    assert fake.writes() == []
    assert any(r.action == "created" for r in records)


def test_api_write_failure_names_the_failing_object():
    from erclient.er_errors import ERClientException

    from er_events_cli.apply import ApplyError

    fake = FakeER()

    def boom(event_type, version="v1.0"):
        raise ERClientException("400 schema invalid")

    fake.post_event_type = boom
    with pytest.raises(ApplyError) as exc:
        apply_spec(fake, _spec())
    assert "creating event_type 'sighting': 400 schema invalid" in str(exc.value)
```

(add `import pytest` to the imports at the top of `tests/test_apply.py`)

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/test_apply.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'er_events_cli.apply'`

- [ ] **Step 4: Implement `apply.py`**

```python
"""Orchestrate applying a Spec: category -> choices -> event types.

apply only manages what the spec declares: server objects absent from the
spec are never touched, EXCEPT choice options within a spec-managed field
(the spec is authoritative for a field's option set; removed options are
deactivated, never deleted).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from erclient.er_errors import ERClientException

from . import client as er
from .choices import desired_choice_records, plan_field_choices
from .dsl import Spec
from .schema_gen import build_event_type_payload

_REF_FIELD_RE = re.compile(r"choices\.json\?field=([^&\"']+)")


class ApplyError(Exception):
    """An API write failed; the message names the failing object."""


def _write(description: str, fn, *args, **kwargs):
    """Run one API write; on failure, raise ApplyError naming what we were doing."""
    try:
        return fn(*args, **kwargs)
    except ERClientException as e:
        raise ApplyError(f"{description}: {e}") from e


@dataclass
class ActionRecord:
    kind: str  # "category" | "choice" | "event_type"
    name: str
    action: str  # "created" | "updated" | "unchanged" | "deactivated"
    detail: str = ""


def normalize_v2_schema(schema: dict) -> dict:
    """Repair a GET'd v2 schema for comparison/PATCH: ER's GET returns
    additionalProperties where its POST meta-schema requires unevaluatedProperties."""
    if not isinstance(schema, dict):
        return schema
    json_block = schema.get("json")
    if not isinstance(json_block, dict):
        return schema
    if "unevaluatedProperties" not in json_block:
        legacy = json_block.get("additionalProperties")
        json_block["unevaluatedProperties"] = legacy if isinstance(legacy, bool) else False
    json_block.pop("additionalProperties", None)
    return schema


def extract_choice_fields(schema: dict) -> list[str]:
    """Choice-field names referenced by $ref anywhere in a v2 schema, in order."""
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                m = _REF_FIELD_RE.search(ref)
                if m and m.group(1) not in found:
                    found.append(m.group(1))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return found


def apply_spec(client, spec: Spec, dry_run: bool = False) -> list[ActionRecord]:
    records = [_apply_category(client, spec, dry_run)]
    for et in spec.event_types:
        for field_name, desired in desired_choice_records(et).items():
            records.extend(_apply_field_choices(client, field_name, desired, dry_run))
    existing_types = client.get_event_types(
        include_inactive=True, include_schema=True, version="v2.0"
    )
    existing_by_value = {t.get("value"): t for t in existing_types}
    for et in spec.event_types:
        records.append(
            _apply_event_type(
                client, et, spec.category.value, existing_by_value.get(et.value), dry_run
            )
        )
    return records


def _apply_category(client, spec: Spec, dry_run: bool) -> ActionRecord:
    existing = client.get_event_categories(include_inactive=True)
    cat = next((c for c in existing if c.get("value") == spec.category.value), None)
    if cat is None:
        if not dry_run:
            _write(
                f"creating category {spec.category.value!r}",
                client.post_event_category,
                {"value": spec.category.value, "display": spec.category.display},
            )
        return ActionRecord("category", spec.category.value, "created")
    if cat.get("display") != spec.category.display:
        if not dry_run:
            _write(
                f"updating category {spec.category.value!r}",
                client.patch_event_category,
                {"id": cat["id"], "value": spec.category.value, "display": spec.category.display},
            )
        return ActionRecord(
            "category", spec.category.value, "updated",
            detail=f"display -> {spec.category.display!r}",
        )
    return ActionRecord("category", spec.category.value, "unchanged")


def _apply_field_choices(client, field_name: str, desired: list[dict], dry_run: bool):
    existing = er.get_choices(client, field_name)
    action_names = {"create": "created", "update": "updated",
                    "deactivate": "deactivated", "unchanged": "unchanged"}
    records = []
    for op in plan_field_choices(existing, desired):
        if op.action == "create" and not dry_run:
            _write(f"creating choice '{field_name}:{op.value}'", er.post_choice, client, op.payload)
        elif op.action in ("update", "deactivate") and not dry_run:
            _write(
                f"updating choice '{field_name}:{op.value}'",
                er.patch_choice, client, op.choice_id, op.payload,
            )
        records.append(
            ActionRecord("choice", f"{field_name}:{op.value}", action_names[op.action])
        )
    return records


def _category_value(raw: object) -> str | None:
    if isinstance(raw, dict):
        return raw.get("value")
    return raw


def _event_type_differs(payload: dict, existing: dict) -> bool:
    if payload["display"] != existing.get("display"):
        return True
    if payload["is_active"] != existing.get("is_active", True):
        return True
    if payload["category"] != _category_value(existing.get("category")):
        return True
    if "icon_id" in payload and payload["icon_id"] != existing.get("icon_id"):
        return True
    return payload["schema"] != normalize_v2_schema(existing.get("schema") or {})


def _apply_event_type(client, et, category_value: str, existing: dict | None, dry_run: bool):
    payload = build_event_type_payload(et, category_value)
    if existing is None:
        if not dry_run:
            _write(
                f"creating event_type {et.value!r}",
                client.post_event_type, payload, version="v2.0",
            )
        return ActionRecord("event_type", et.value, "created")
    if _event_type_differs(payload, existing):
        patch = {**payload, "id": existing.get("id")}
        if not dry_run:
            _write(
                f"updating event_type {et.value!r}",
                client.patch_event_type, patch, version="v2.0",
            )
        return ActionRecord("event_type", et.value, "updated")
    return ActionRecord("event_type", et.value, "unchanged")
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_apply.py -v`
Expected: all PASS. Then run the whole suite: `pytest -v` — all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/er_events_cli/apply.py tests/conftest.py tests/test_apply.py
git commit -m "feat: idempotent apply orchestration with dry-run

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Event building and posting (`events.py`)

**Files:**
- Create: `src/er_events_cli/events.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Consumes: `FakeER` from conftest (tests only).
- Produces:
  - `class FieldArgError(ValueError)`
  - `parse_field_args(pairs: list[str]) -> dict` — `"k=v"` strings; values YAML-coerced
  - `build_event(*, event_type: str, details: dict, location: str | None = None, time: str | None = None, title: str | None = None) -> dict`
  - `load_events_file(path: str) -> list[dict]`
  - `post_events(client, events: list[dict]) -> list[str | None]` — per-event `None` on success, error text on failure

- [ ] **Step 1: Write failing tests**

`tests/test_events.py`:

```python
import pytest
from conftest import FakeER

from er_events_cli.events import (
    FieldArgError,
    build_event,
    load_events_file,
    parse_field_args,
    post_events,
)


def test_parse_field_args_yaml_coercion():
    details = parse_field_args(["species=elephant", "count=3", "injured=true", "threats=[a, b]"])
    assert details == {"species": "elephant", "count": 3, "injured": True, "threats": ["a", "b"]}


def test_parse_field_args_rejects_missing_equals():
    with pytest.raises(FieldArgError):
        parse_field_args(["species"])


def test_build_event_minimal_defaults_time():
    event = build_event(event_type="sighting", details={"species": "elephant"})
    assert event["event_type"] == "sighting"
    assert event["event_details"] == {"species": "elephant"}
    assert "time" in event
    assert "location" not in event


def test_build_event_with_location_time_title():
    event = build_event(
        event_type="sighting", details={}, location="-1.286,36.817",
        time="2026-08-31T12:00:00Z", title="Morning patrol",
    )
    assert event["location"] == {"latitude": -1.286, "longitude": 36.817}
    assert event["time"] == "2026-08-31T12:00:00Z"
    assert event["title"] == "Morning patrol"


def test_build_event_bad_location():
    with pytest.raises(FieldArgError):
        build_event(event_type="s", details={}, location="nowhere")


def test_load_events_file(tmp_path):
    p = tmp_path / "events.yaml"
    p.write_text(
        "- event_type: sighting\n"
        "  event_details: {species: elephant}\n"
        "  location: {latitude: -1.0, longitude: 36.0}\n"
        "- event_type: sighting\n"
        "  event_details: {species: lion}\n"
    )
    events = load_events_file(str(p))
    assert len(events) == 2
    assert events[0]["location"] == {"latitude": -1.0, "longitude": 36.0}
    assert all("time" in e for e in events)


def test_load_events_file_rejects_non_list(tmp_path):
    p = tmp_path / "events.yaml"
    p.write_text("event_type: sighting\n")
    with pytest.raises(FieldArgError):
        load_events_file(str(p))


def test_post_events_reports_per_event_outcomes():
    fake = FakeER()

    def failing_post(event):
        if event["event_details"]["species"] == "lion":
            raise RuntimeError("400 bad species")
        return event

    fake.post_event = failing_post
    events = [
        {"event_type": "s", "event_details": {"species": "elephant"}},
        {"event_type": "s", "event_details": {"species": "lion"}},
    ]
    outcomes = post_events(fake, events)
    assert outcomes[0] is None
    assert "400 bad species" in outcomes[1]
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_events.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `events.py`**

```python
"""Build and post EarthRanger events."""

from __future__ import annotations

from datetime import datetime, timezone

import yaml


class FieldArgError(ValueError):
    pass


def parse_field_args(pairs: list[str]) -> dict:
    details: dict = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise FieldArgError(f"--field expects key=value, got {pair!r}")
        details[key] = yaml.safe_load(raw) if raw != "" else ""
    return details


def build_event(
    *,
    event_type: str,
    details: dict,
    location: str | None = None,
    time: str | None = None,
    title: str | None = None,
) -> dict:
    event: dict = {
        "event_type": event_type,
        "time": time or datetime.now(timezone.utc).isoformat(),
        "event_details": details,
    }
    if location:
        lat_str, _, lon_str = location.partition(",")
        try:
            event["location"] = {"latitude": float(lat_str), "longitude": float(lon_str)}
        except ValueError:
            raise FieldArgError(f"--location expects LAT,LON, got {location!r}") from None
    if title:
        event["title"] = title
    return event


def load_events_file(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, list):
        raise FieldArgError("events file must be a YAML list of event objects")
    events: list[dict] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or "event_type" not in item:
            raise FieldArgError(f"events[{i}] must be a mapping with an 'event_type'")
        event = dict(item)
        event.setdefault("time", datetime.now(timezone.utc).isoformat())
        events.append(event)
    return events


def post_events(client, events: list[dict]) -> list[str | None]:
    """Post each event; one entry per event: None on success, error text on failure."""
    outcomes: list[str | None] = []
    for event in events:
        try:
            client.post_event(event)
            outcomes.append(None)
        except Exception as e:  # ERClientException subclasses or transport errors
            outcomes.append(str(e))
    return outcomes
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_events.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/er_events_cli/events.py tests/test_events.py
git commit -m "feat: event payload building and batch posting

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: CLI — group, connection, `apply` command (`cli.py`)

**Files:**
- Create: `src/er_events_cli/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `apply_spec`/`ActionRecord` from `apply`; `SpecError`, `load_spec` from `dsl`; `make_client` from `client`.
- Produces: Click group `main` (the console-script entry point) with global options `--server/--username/--password` (envvars `ER_SERVER`/`ER_USERNAME`/`ER_PASSWORD`), helper `_connect(ctx)`, command `apply` with `--dry-run`. Task 10 adds `post-event`, `list`, `show`.

- [ ] **Step 1: Write failing tests**

`tests/test_cli.py`:

```python
import pytest
from click.testing import CliRunner
from conftest import FakeER

import er_events_cli.cli as cli_mod
from er_events_cli.cli import main

SPEC_YAML = """
category: {value: wm, display: Wildlife Monitoring}
event_types:
  - value: sighting
    display: Sighting
    fields:
      - {key: notes, label: Notes, type: string}
"""

BAD_SPEC_YAML = """
category: {value: wm}
event_types: []
"""


@pytest.fixture
def fake(monkeypatch):
    fake = FakeER()
    monkeypatch.setattr(cli_mod, "_connect", lambda ctx: fake)
    return fake


def _run(args, spec_text=SPEC_YAML, input=None):
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("spec.yaml", "w") as f:
            f.write(spec_text)
        return runner.invoke(main, args, input=input, catch_exceptions=False)


def test_apply_fresh_site(fake):
    result = _run(["apply", "spec.yaml"])
    assert result.exit_code == 0
    assert "category    created      wm" in result.output
    assert "event_type  created      sighting" in result.output
    assert any(c[0] == "post_event_type" for c in fake.calls)


def test_apply_dry_run_prefixes_and_writes_nothing(fake):
    result = _run(["apply", "spec.yaml", "--dry-run"])
    assert result.exit_code == 0
    assert "would-created" in result.output
    assert fake.writes() == []


def test_apply_bad_spec_lists_all_errors_and_exits_1(fake):
    result = _run(["apply", "spec.yaml"], spec_text=BAD_SPEC_YAML)
    assert result.exit_code == 1
    assert "category.display: required string" in result.output
    assert "event_types: at least one event type is required" in result.output
    assert fake.calls == []


def test_connect_requires_server(monkeypatch):
    monkeypatch.delenv("ER_SERVER", raising=False)
    monkeypatch.delenv("ER_USERNAME", raising=False)
    monkeypatch.delenv("ER_PASSWORD", raising=False)
    result = _run(["apply", "spec.yaml"])
    assert result.exit_code != 0
    assert "Missing server" in result.output


def test_connect_prompts_for_password(monkeypatch):
    captured = {}

    def fake_make_client(*, server, username, password):
        captured.update(server=server, username=username, password=password)
        return FakeER()

    monkeypatch.setattr(cli_mod, "make_client", fake_make_client)
    result = _run(
        ["--server", "myreserve", "--username", "u", "apply", "spec.yaml"],
        input="secret\n",
    )
    assert result.exit_code == 0
    assert captured == {"server": "myreserve", "username": "u", "password": "secret"}


def test_api_errors_print_cleanly_and_exit_1(fake):
    from erclient.er_errors import ERClientBadCredentials

    def bad_creds(include_inactive=False):
        raise ERClientBadCredentials("Invalid credentials given.")

    fake.get_event_categories = bad_creds
    result = _run(["apply", "spec.yaml"])
    assert result.exit_code == 1
    assert "error: Invalid credentials given." in result.output
```

Note: `test_connect_requires_server` does NOT use the `fake` fixture — it exercises the real `_connect`. `test_api_errors_print_cleanly_and_exit_1` needs `catch_exceptions` left at Click's default for `sys.exit` to surface as `exit_code` — change `_run` to pass `catch_exceptions=True` is NOT needed; `CliRunner.invoke` treats `SystemExit` specially even with `catch_exceptions=False`.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'er_events_cli.cli'`

- [ ] **Step 3: Implement `cli.py`**

```python
"""Click CLI wiring. Logic lives in the other modules; this file only parses
flags, connects, and formats output."""

from __future__ import annotations

import functools
import sys

import click
from erclient.er_errors import ERClientException

from .apply import ApplyError, apply_spec
from .client import make_client
from .dsl import SpecError, load_spec


def _api_errors(f):
    """Turn API failures (auth, writes) into a clean message and exit 1."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except (ApplyError, ERClientException) as e:
            click.echo(f"error: {e}")
            sys.exit(1)

    return wrapper


def _connect(ctx):
    server = ctx.obj["server"]
    username = ctx.obj["username"]
    password = ctx.obj["password"]
    if not server:
        raise click.UsageError("Missing server: pass --server or set ER_SERVER.")
    if not username:
        raise click.UsageError("Missing username: pass --username or set ER_USERNAME.")
    if not password:
        password = click.prompt("Password", hide_input=True)
    return make_client(server=server, username=username, password=password)


@click.group()
@click.option("--server", envvar="ER_SERVER",
              help="ER site name (myreserve) or full https:// URL.")
@click.option("--username", envvar="ER_USERNAME", help="EarthRanger username.")
@click.option("--password", envvar="ER_PASSWORD",
              help="EarthRanger password (prompted if omitted).")
@click.pass_context
def main(ctx, server, username, password):
    """Create and edit EarthRanger event categories, choices, and v2 event types."""
    ctx.obj = {"server": server, "username": username, "password": password}


@main.command("apply")
@click.argument("spec_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--dry-run", is_flag=True, help="Show planned changes without writing.")
@click.pass_context
@_api_errors
def apply_cmd(ctx, spec_file, dry_run):
    """Create or update the category, choices, and event types in SPEC_FILE."""
    try:
        spec = load_spec(spec_file)
    except SpecError as e:
        for err in e.errors:
            click.echo(f"error: {err}")
        sys.exit(1)
    client = _connect(ctx)
    for r in apply_spec(client, spec, dry_run=dry_run):
        action = r.action
        if dry_run and action != "unchanged":
            action = f"would-{action}"
        line = f"{r.kind:<11} {action:<12} {r.name}"
        if r.detail:
            line += f"  ({r.detail})"
        click.echo(line)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cli.py -v`
Expected: all PASS. Also verify the console script exists: `er-events --help` prints the group help.

- [ ] **Step 5: Commit**

```bash
git add src/er_events_cli/cli.py tests/test_cli.py
git commit -m "feat: CLI group, connection resolution, and apply command

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: CLI — `post-event`, `list`, `show` commands

**Files:**
- Modify: `src/er_events_cli/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `parse_field_args`, `build_event`, `load_events_file`, `post_events`, `FieldArgError` from `events`; `extract_choice_fields` from `apply`; `get_choices` from `client`.
- Produces: commands `post-event`, `list categories`, `list event-types [--category]`, `show event-type VALUE`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_cli.py`:

```python
import json


def test_post_event_flags(fake):
    result = _run([
        "post-event", "--event-type", "sighting",
        "--field", "species=elephant", "--field", "count=3",
        "--location", "-1.286,36.817", "--title", "Morning",
    ])
    assert result.exit_code == 0
    assert "posted   sighting" in result.output
    posted = next(c[1] for c in fake.calls if c[0] == "post_event")
    assert posted["event_details"] == {"species": "elephant", "count": 3}
    assert posted["location"] == {"latitude": -1.286, "longitude": 36.817}
    assert posted["title"] == "Morning"


def test_post_event_requires_type_or_file(fake):
    result = _run(["post-event"])
    assert result.exit_code != 0


def test_post_event_batch_partial_failure_exits_1(fake):
    def failing_post(event):
        if event["event_details"].get("species") == "lion":
            raise RuntimeError("boom")
        return event

    fake.post_event = failing_post
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("events.yaml", "w") as f:
            f.write(
                "- event_type: s\n  event_details: {species: elephant}\n"
                "- event_type: s\n  event_details: {species: lion}\n"
            )
        result = runner.invoke(main, ["post-event", "--file", "events.yaml"])
    assert result.exit_code == 1
    assert "posted   s" in result.output
    assert "FAILED   s: boom" in result.output


def test_list_categories(fake):
    fake.categories = [
        {"value": "wm", "display": "Wildlife Monitoring", "is_active": True},
        {"value": "old", "display": "Old Category", "is_active": False},
    ]
    result = _run(["list", "categories"])
    assert result.exit_code == 0
    assert "wm" in result.output
    assert "(inactive)" in result.output


def test_list_event_types_filters_by_category(fake):
    fake.event_types = [
        {"value": "a", "display": "A", "category": {"value": "wm"}, "is_active": True},
        {"value": "b", "display": "B", "category": "other", "is_active": True},
    ]
    result = _run(["list", "event-types", "--category", "wm"])
    assert result.exit_code == 0
    assert "a" in result.output
    assert " b " not in result.output


def test_show_event_type_includes_choices(fake):
    schema = {"json": {"properties": {
        "species": {"anyOf": [{"$ref": "/api/v2.0/schemas/choices.json?field=s_species"}]}
    }}}
    fake.event_types = [
        {"value": "s", "display": "S", "category": "wm", "schema": schema},
    ]
    fake.choices = {"s_species": [{"id": "1", "value": "elephant", "display": "Elephant"}]}
    result = _run(["show", "event-type", "s"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["event_type"]["value"] == "s"
    assert data["choices"]["s_species"][0]["value"] == "elephant"


def test_show_event_type_missing_exits_1(fake):
    result = _run(["show", "event-type", "nope"])
    assert result.exit_code == 1
    assert "no event type with value 'nope'" in result.output
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_cli.py -v`
Expected: new tests FAIL (`no such command`)

- [ ] **Step 3: Implement — append to `cli.py`**

Add imports at the top of the file:

```python
import json

from . import client as er
from .apply import extract_choice_fields
from .events import FieldArgError, build_event, load_events_file, parse_field_args, post_events
```

Append the commands:

```python
@main.command("post-event")
@click.option("--event-type", "event_type", help="Event type value (required unless --file).")
@click.option("--field", "fields", multiple=True,
              help="key=value; value parsed as a YAML scalar. Repeatable.")
@click.option("--location", help="LAT,LON")
@click.option("--time", "time_", help="ISO-8601 timestamp; defaults to now (UTC).")
@click.option("--title", help="Event title.")
@click.option("--file", "file_", type=click.Path(exists=True, dir_okay=False),
              help="YAML list of events to post.")
@click.pass_context
@_api_errors
def post_event_cmd(ctx, event_type, fields, location, time_, title, file_):
    """Post one event (via flags) or a batch (via --file)."""
    try:
        if file_:
            events = load_events_file(file_)
        elif event_type:
            events = [build_event(event_type=event_type,
                                  details=parse_field_args(list(fields)),
                                  location=location, time=time_, title=title)]
        else:
            raise click.UsageError("Pass --event-type (with --field ...) or --file.")
    except FieldArgError as e:
        click.echo(f"error: {e}")
        sys.exit(1)
    client = _connect(ctx)
    failures = 0
    for event, error in zip(events, post_events(client, events)):
        if error is None:
            click.echo(f"posted   {event['event_type']}")
        else:
            failures += 1
            click.echo(f"FAILED   {event['event_type']}: {error}")
    if failures:
        sys.exit(1)


@main.group("list")
def list_group():
    """List objects on the server."""


@list_group.command("categories")
@click.pass_context
@_api_errors
def list_categories(ctx):
    """List event categories (inactive included)."""
    client = _connect(ctx)
    for c in client.get_event_categories(include_inactive=True):
        active = "" if c.get("is_active", True) else "  (inactive)"
        click.echo(f"{c.get('value'):<40} {c.get('display')}{active}")


def _category_value_of(event_type: dict):
    raw = event_type.get("category")
    return raw.get("value") if isinstance(raw, dict) else raw


@list_group.command("event-types")
@click.option("--category", help="Filter by category value.")
@click.pass_context
@_api_errors
def list_event_types(ctx, category):
    """List event types (inactive included)."""
    client = _connect(ctx)
    for t in client.get_event_types(include_inactive=True, version="v2.0"):
        cat_value = _category_value_of(t)
        if category and cat_value != category:
            continue
        active = "" if t.get("is_active", True) else "  (inactive)"
        click.echo(f"{t.get('value'):<40} {t.get('display'):<40} {cat_value}{active}")


@main.group("show")
def show_group():
    """Show one object in full."""


@show_group.command("event-type")
@click.argument("value")
@click.pass_context
@_api_errors
def show_event_type(ctx, value):
    """Print the full v2 event type JSON plus its referenced Choice records."""
    client = _connect(ctx)
    types = client.get_event_types(include_inactive=True, include_schema=True, version="v2.0")
    et = next((t for t in types if t.get("value") == value), None)
    if et is None:
        click.echo(f"error: no event type with value {value!r}")
        sys.exit(1)
    fields = extract_choice_fields(et.get("schema") or {})
    choices = {f: er.get_choices(client, f) for f in fields}
    click.echo(json.dumps({"event_type": et, "choices": choices}, indent=2))
```

Note for `test_post_event_batch_partial_failure_exits_1`: it invokes `main` without the `fake` fixture's monkeypatch applying inside `runner.isolated_filesystem()` — the fixture patch is process-wide, so it still applies; the test is correct as written.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cli.py -v` then `pytest -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/er_events_cli/cli.py tests/test_cli.py
git commit -m "feat: post-event, list, and show commands

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 11: Examples, README, final verification

**Files:**
- Create: `examples/wildlife_monitoring.yaml`, `examples/events.yaml`, `README.md`

**Interfaces:**
- Consumes: everything; the example spec must pass `load_spec` (add the test below).

- [ ] **Step 1: Write the example spec**

`examples/wildlife_monitoring.yaml`:

```yaml
# Example spec for `er-events apply` — one category, one event type
# exercising every supported field type.
#
# Slugs (category value, event type values, field keys, option values)
# must match [a-z0-9_]+. Quote anything YAML might reinterpret
# (e.g. "no", "1.10") — see "Why YAML?" in the README.

category:
  value: wildlife_monitoring
  display: Wildlife Monitoring

event_types:
  - value: animal_sighting
    display: Animal Sighting
    icon_id: mammal_rep          # optional; must exist on your ER site
    fields:
      - key: species
        label: Species
        type: select             # becomes shared Choice records + a $ref
        options:
          - {value: elephant, display: Elephant}
          - lion                 # shorthand: value=lion, display="Lion"
          - {value: giraffe, display: Giraffe}
      - key: count
        label: Number of animals
        type: integer
        min: 0
      - key: adult_ratio
        label: Adult ratio
        type: number
        min: 0
        max: 1
      - key: injured
        label: Injured animal present
        type: boolean
      - key: seen_on
        label: Date seen
        type: date
      - key: exact_time
        label: Exact time
        type: datetime
      - key: threats
        label: Observed threats
        type: multiselect        # array of choice values
        options: [poaching, snares, habitat_loss]
      - key: observer
        label: Observer name
        type: string
      - key: notes
        label: Notes
        type: textarea
    required: [species]
```

`examples/events.yaml`:

```yaml
# Example batch file for `er-events post-event --file events.yaml`.
# Each entry: event_type (required), event_details, optional
# location / time / title. time defaults to now (UTC).

- event_type: animal_sighting
  title: Morning elephant sighting
  location: {latitude: -1.286, longitude: 36.817}
  event_details:
    species: elephant
    count: 3
    threats: [snares]

- event_type: animal_sighting
  event_details:
    species: lion
    count: 1
```

- [ ] **Step 2: Add a test that the shipped example parses**

Append to `tests/test_dsl.py`:

```python
def test_shipped_example_spec_parses():
    import pathlib

    example = pathlib.Path(__file__).parent.parent / "examples" / "wildlife_monitoring.yaml"
    spec = load_spec(str(example))
    assert spec.category.value == "wildlife_monitoring"
    assert {f.type for f in spec.event_types[0].fields} == {
        "select", "integer", "number", "boolean", "date", "datetime",
        "multiselect", "string", "textarea",
    }
```

Run: `pytest tests/test_dsl.py -v` — PASS.

- [ ] **Step 3: Write `README.md`**

```markdown
# er-events-cli

A command-line utility for creating and **editing** EarthRanger event
categories, choices, and v2 event types — and posting events — directly
against the EarthRanger API, authenticated with a username and password.

You describe what you want in a small YAML spec (no hand-written JSON
Schema); `er-events apply` generates the ER v2 schema envelope and the
shared Choice records, then idempotently creates what's missing and
patches what changed. Nothing is ever deleted — removal means
`is_active: false`.

## Install

```bash
uv pip install -e .
```

## Authenticate

Every command needs a server and credentials:

```bash
export ER_SERVER=myreserve          # site name, or a full https:// URL
export ER_USERNAME=me
export ER_PASSWORD=...              # omit to be prompted interactively
```

or pass `--server/--username/--password` before the subcommand.

## Walkthrough

1. Write a spec (start from `examples/wildlife_monitoring.yaml`):

   ```yaml
   category:
     value: wildlife_monitoring
     display: Wildlife Monitoring
   event_types:
     - value: animal_sighting
       display: Animal Sighting
       fields:
         - key: species
           label: Species
           type: select
           options: [elephant, lion]
         - key: count
           label: Number of animals
           type: integer
           min: 0
       required: [species]
   ```

2. Preview what would change, then apply:

   ```bash
   er-events apply spec.yaml --dry-run
   er-events apply spec.yaml
   ```

3. Post an event against the new type:

   ```bash
   er-events post-event --event-type animal_sighting \
       --field species=elephant --field count=3 \
       --location -1.286,36.817
   ```

4. Edit: change the spec (rename a display, add a field, drop an
   option), dry-run again, re-apply. `apply` patches exactly what
   differs; dropped options are deactivated, never deleted.

## Commands

| Command | What it does |
|---|---|
| `apply SPEC [--dry-run]` | Upsert category, choices, and event types from a spec |
| `post-event --event-type V --field k=v ...` | Post one event (`--location LAT,LON`, `--time`, `--title`) |
| `post-event --file events.yaml` | Post a batch; exits 1 if any fail |
| `list categories` | List categories (inactive included) |
| `list event-types [--category V]` | List event types |
| `show event-type V` | Full v2 event-type JSON + its Choice records |

## Spec reference

Field types: `string`, `textarea`, `integer`, `number` (both take
optional `min`/`max`), `boolean`, `date`, `datetime`, `select`,
`multiselect` (both take `options`).

Options are `{value, display}` mappings or bare strings
(`lion` → value `lion`, display `Lion`). Slugs — category value, event
type values, field keys, option values — must match `[a-z0-9_]+`.

Per event type: `value`, `display`, `fields` (required);
`required`, `is_active`, `icon_id` (optional).

### What apply owns

`apply` only manages what the spec declares: it never touches event
types or categories that exist on the server but aren't in the spec
(retire one by setting `is_active: false` in the spec). The exception
is choice options within a spec-managed field — there the spec is
authoritative, and removed options are deactivated on the server.

### Why YAML and not JSON?

Both work: spec files are parsed with a YAML parser, and YAML is a
superset of JSON, so a pure-JSON spec file is accepted as-is. YAML is
the documented format because spec files are hand-authored and reviewed
— comments matter, and block style keeps nested fields readable. Watch
YAML's implicit typing (`no` → false, `1.10` → a float): quote anything
ambiguous.

## v2 schemas and choices, briefly

ER v2 event types store a `{json, ui}` schema envelope. Dropdown fields
don't embed their options; they reference shared **Choice** records via
`$ref: /api/v2.0/schemas/choices.json?field=<name>`. This tool derives
`<name>` as `<event_type_value>_<field_key>` and manages those records
for you — creating, re-labelling, deactivating, and reactivating options
to mirror your spec.
```

- [ ] **Step 4: Final verification**

Run: `pytest -v` — all tests PASS.
Run: `ruff check src tests` — no findings (fix any it reports).
Run: `ruff format --check src tests` — clean (run `ruff format src tests` if not).
Run: `er-events --help` — shows all four commands.

- [ ] **Step 5: Commit**

```bash
git add examples README.md tests/test_dsl.py
git commit -m "docs: README, example spec, and batch-events example

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
