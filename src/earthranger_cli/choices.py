"""Choice-record planning: desired records from a spec, diff against existing. Pure."""

from __future__ import annotations

from dataclasses import dataclass

from .client import CHOICE_MODEL
from .dsl import OptionSpec, Spec
from .schema_gen import VARCHAR_LIMIT, effective_choice_field


@dataclass
class ChoiceOp:
    action: str  # "create" | "update" | "deactivate" | "unchanged"
    value: str
    payload: dict | None = None  # POST/PATCH body; None for unchanged
    choice_id: str | None = None  # set for update/deactivate


def _records(name: str, options: list[OptionSpec]) -> list[dict]:
    out: list[dict] = []
    for i, o in enumerate(options):
        rec = {
            "model": CHOICE_MODEL,
            "field": name,
            "value": o.value[:VARCHAR_LIMIT],
            "display": o.display[:VARCHAR_LIMIT],
            "is_active": True,
            "ordernum": i,  # spec order is authoritative for dropdown order
        }
        if o.icon:
            rec["icon"] = o.icon[:VARCHAR_LIMIT]
        out.append(rec)
    return out


def desired_choice_sets(spec: Spec) -> dict[str, list[dict]]:
    """Every choice set the spec declares, exactly once per Choice.field name:
    top-level sets plus per-field inline options (a shared inline set — dsl
    validation guarantees identical options — is captured once)."""
    sets = {name: _records(name, opts) for name, opts in spec.choices.items()}
    for et in spec.event_types:
        for f in et.fields:
            if f.options is None:
                continue
            name = effective_choice_field(et.value, f)
            if name in sets:
                continue
            sets[name] = _records(name, f.options)
    return sets


def plan_field_choices(existing: list[dict], desired: list[dict]) -> list[ChoiceOp]:
    ops: list[ChoiceOp] = []
    existing_by_value = {c["value"]: c for c in existing}
    desired_values = {d["value"] for d in desired}
    # When the visible (active, display-ordered) sequence already matches the
    # spec and every active record has an ordernum, leave the server's
    # numbering alone: renumbering would be a write with no visible effect
    # (e.g. stock 10/20/30 gaps, or gaps left by deactivated records).
    active = [c for c in existing if c.get("is_active", True)]
    order_settled = [c["value"] for c in sorted(active, key=choice_sort_key)] == [
        d["value"] for d in desired
    ] and all(c.get("ordernum") is not None for c in active)
    for want in desired:
        have = existing_by_value.get(want["value"])
        if have is None:
            ops.append(ChoiceOp(action="create", value=want["value"], payload=want))
        elif _differs(have, want, order_settled):
            payload = {"display": want["display"], "is_active": True}
            if not order_settled and have.get("ordernum") != want.get("ordernum"):
                payload["ordernum"] = want.get("ordernum")
            if (have.get("icon") or None) != (want.get("icon") or None):
                payload["icon"] = want.get("icon")
            ops.append(
                ChoiceOp(
                    action="update",
                    value=want["value"],
                    payload=payload,
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


def choice_sort_key(record: dict):
    """Server display order: ordernum ascending, nulls last, then value."""
    num = record.get("ordernum")
    return (num is None, num if num is not None else 0, record.get("value") or "")


def _differs(have: dict, want: dict, order_settled: bool) -> bool:
    return (
        have.get("display") != want["display"]
        or not have.get("is_active", True)
        or (not order_settled and have.get("ordernum") != want.get("ordernum"))
        or (have.get("icon") or None) != (want.get("icon") or None)
    )
