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
