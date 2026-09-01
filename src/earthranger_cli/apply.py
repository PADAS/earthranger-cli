"""Orchestrate applying a Spec: category -> choices -> event types.

apply only manages what the spec declares: server objects absent from the
spec are never touched, EXCEPT choice options within a spec-managed field
(the spec is authoritative for a field's option set; removed options are
deactivated, never deleted).
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass

from erclient.er_errors import ERClientException

from . import client as er
from .choices import desired_choice_sets, plan_field_choices
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


def normalize_v2_schema(schema: dict | str) -> dict | str:
    """Repair a GET'd v2 schema for comparison/PATCH: ER sometimes returns the
    schema JSON-stringified, and its GET returns additionalProperties where the
    POST meta-schema requires unevaluatedProperties."""
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except (json.JSONDecodeError, TypeError):
            return schema
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
    """Choice-field names referenced by $ref anywhere in a v2 schema, in
    order."""
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
    for field_name, desired in desired_choice_sets(spec).items():
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
            "category",
            spec.category.value,
            "updated",
            detail=f"display -> {spec.category.display!r}",
        )
    return ActionRecord("category", spec.category.value, "unchanged")


def _apply_field_choices(client, field_name: str, desired: list[dict], dry_run: bool):
    existing = er.get_choices(client, field_name)
    action_names = {
        "create": "created",
        "update": "updated",
        "deactivate": "deactivated",
        "unchanged": "unchanged",
    }
    records = []
    for op in plan_field_choices(existing, desired):
        if op.action == "create" and not dry_run:
            _write(f"creating choice '{field_name}:{op.value}'", er.post_choice, client, op.payload)
        elif op.action in ("update", "deactivate") and not dry_run:
            _write(
                f"updating choice '{field_name}:{op.value}'",
                er.patch_choice,
                client,
                op.choice_id,
                op.payload,
            )
        records.append(ActionRecord("choice", f"{field_name}:{op.value}", action_names[op.action]))
    return records


def _category_value(raw: object) -> str | None:
    if isinstance(raw, dict):
        return raw.get("value")
    return raw


def _canonical_schema(schema) -> object:
    """Comparison form of a schema: normalized, with server-echo noise removed
    (schema-level icon_id/image_url that ER injects on GET, and empty-string
    description/placeholder, which are wire-equivalent to omitting the key)."""
    schema = normalize_v2_schema(copy.deepcopy(schema))
    if not isinstance(schema, dict):
        return schema
    schema.pop("icon_id", None)
    schema.pop("image_url", None)
    for prop in (schema.get("json", {}).get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("description") == "":
            del prop["description"]
    for ui_field in (schema.get("ui", {}).get("fields") or {}).values():
        if isinstance(ui_field, dict):
            if ui_field.get("placeholder") == "":
                del ui_field["placeholder"]
            if ui_field.get("conditionalDependents") == []:
                del ui_field["conditionalDependents"]
    for section in (schema.get("ui", {}).get("sections") or {}).values():
        if isinstance(section, dict) and section.get("conditions") == []:
            del section["conditions"]
    return schema


def _event_type_differs(payload: dict, existing: dict) -> bool:
    if payload["display"] != existing.get("display"):
        return True
    if payload["is_active"] != existing.get("is_active", True):
        return True
    if payload["category"] != _category_value(existing.get("category")):
        return True
    if "icon" in payload and payload["icon"] != existing.get("icon"):
        return True
    if payload.get("is_collection", False) != existing.get("is_collection", False):
        return True
    return _canonical_schema(payload["schema"]) != _canonical_schema(existing.get("schema") or {})


def _apply_event_type(client, et, category_value: str, existing: dict | None, dry_run: bool):
    payload = build_event_type_payload(et, category_value)
    if existing is None:
        if not dry_run:
            _write(
                f"creating event_type {et.value!r}",
                client.post_event_type,
                payload,
                version="v2.0",
            )
        return ActionRecord("event_type", et.value, "created")
    if _event_type_differs(payload, existing):
        patch = {**payload, "id": existing.get("id")}
        if not dry_run:
            _write(
                f"updating event_type {et.value!r}",
                client.patch_event_type,
                patch,
                version="v2.0",
            )
        return ActionRecord("event_type", et.value, "updated")
    return ActionRecord("event_type", et.value, "unchanged")
