"""Counting and grouping records in the CLI rather than in the model's head.

The breakdown question ("how many of each priority", "events per month") has
no server-side answer in ER, so the choice is between the CLI counting and the
caller counting; a caller adding up a table is where sums go wrong.
"""

from __future__ import annotations

import click

from . import clock
from .output import cell, lookup, pluck


def group_counts(records: list, field: str) -> tuple[list[dict], int]:
    """Count records per distinct value of `field`. Returns (rows, present):
    `present` is how many records carry the key at all (a null value counts),
    so a caller can tell "the field is not there" from "it is null everywhere".
    Commonest first, then by value, because that is the order the answer is
    read in."""
    counts: dict[str, int] = {}
    matched = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        found, value = lookup(record, field)
        if found:
            matched += 1
        key = cell(value)
        counts[key] = counts.get(key, 0) + 1
    rows = [{"group": key or "(none)", "count": n} for key, n in counts.items()]
    rows.sort(key=lambda row: (-row["count"], str(row["group"])))
    return rows, matched


def scalar_keys(records: list, limit: int = 25) -> list[str]:
    """The field names a caller could group by, for a "no such field" message."""
    keys: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        for key, value in record.items():
            if key not in keys and not isinstance(value, (list, dict)):
                keys.append(key)
    return keys[:limit]


def group_by_period(
    records: list,
    *,
    period: str,
    since: str,
    until: str,
    tz,
    time_field: str,
    naive_tz=None,
) -> tuple[list[dict], int]:
    """Count records into site-local day/week/month buckets over [since, until],
    reading each record's timestamp from the dotted `time_field`. `naive_tz`
    is the zone the endpoint applies to a naive bound (see clock.bucket_window).
    Returns (rows, unbucketed): the server may return records whose timestamp
    is outside the window (a patrol that overlaps it, an event timed after the
    clock instant), and those must be reported rather than silently dropped."""
    buckets = clock.bucket_window(since, until, period, tz, naive_tz=naive_tz)
    rows = [{"period": label, "since": lo, "until": hi, "count": 0} for label, lo, hi in buckets]
    edges = [
        (clock.parse_ts(lo), clock.parse_ts(hi), row) for row, (_, lo, hi) in zip(rows, buckets)
    ]
    unbucketed = 0
    for record in records:
        when = clock.parse_ts(pluck(record, time_field)) if isinstance(record, dict) else None
        for lo, hi, row in edges:
            if when is not None and lo <= when <= hi:
                row["count"] += 1
                break
        else:
            unbucketed += 1
    return rows, unbucketed


def parse_where(items) -> list[tuple[str, str]]:
    """`--where species=buffalo` -> [("species", "buffalo")]; malformed is a usage error."""
    pairs = []
    for item in items or ():
        key, sep, value = str(item).partition("=")
        if not sep or not key.strip() or not value.strip():
            raise click.UsageError(
                f"--where takes KEY=VALUE, e.g. --where species=buffalo; got {item!r}."
            )
        pairs.append((key.strip(), value.strip()))
    return pairs


def _matches(value, wanted: str) -> bool:
    if isinstance(value, dict):
        return any(_matches(value.get(k), wanted) for k in ("name", "value", "display"))
    if isinstance(value, list):
        return any(_matches(v, wanted) for v in value)
    if value is None:
        return False
    return str(value).casefold() == wanted.casefold()


def details_match(record: dict, key: str, value: str) -> bool | None:
    """Case-insensitive match against event_details[key]; a choice dict matches
    on its name, value or display; a list matches if any element does. None
    when the record carries no such detail at all."""
    details = record.get("event_details") if isinstance(record, dict) else None
    if not isinstance(details, dict) or key not in details:
        return None
    return _matches(details[key], value)


def filter_details(records: list, where: list[tuple[str, str]]) -> tuple[list, str]:
    """Records whose event_details match every KEY=VALUE, and a note that names
    the event types carrying no such detail at all (a type can say "elephant"
    in its name instead of in a species field; those cannot match and should
    be counted by type, not as none)."""
    kept = []
    silent: dict[str, dict[str, int]] = {k: {} for k, _ in where}
    for r in records:
        ok = True
        for key, value in where:
            m = details_match(r, key, value)
            if m is None:
                t = str(r.get("event_type") or "?") if isinstance(r, dict) else "?"
                silent[key][t] = silent[key].get(t, 0) + 1
            if not m:
                ok = False
        if ok:
            kept.append(r)
    spec = ", ".join(f"{k}={v}" for k, v in where)
    parts = [
        (
            f"--where {spec} was applied here, to event_details: "
            f"{len(kept)} of {len(records)} event(s) matched."
        )
    ]
    for key, types in silent.items():
        if types:
            top = sorted(types.items(), key=lambda kv: -kv[1])[:6]
            named = ", ".join(f"{t} ({n})" for t, n in top)
            parts.append(
                f"{sum(types.values())} event(s) have no {key!r} detail at all ({named}); "
                "they cannot match it, so count those by event type instead."
            )
    return kept, " ".join(parts)
