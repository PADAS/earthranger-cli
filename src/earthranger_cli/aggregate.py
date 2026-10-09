"""Counting and grouping records in the CLI rather than in the model's head.

The breakdown question ("how many of each priority", "events per month") has
no server-side answer in ER, so the choice is between the CLI counting and the
caller counting; a caller adding up a table is where sums go wrong.
"""

from __future__ import annotations

import bisect

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
        # a record without the key is not the same as one where it is null
        key = (cell(value) or "(none)") if found else "(missing)"
        counts[key] = counts.get(key, 0) + 1
    rows = [{"group": key, "count": n} for key, n in counts.items()]
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
    rows = [
        {"period": label, "since": lo, "until": hi, "count": 0, "partial": partial}
        for label, lo, hi, partial in buckets
    ]
    edges = [
        (clock.parse_ts(lo), clock.parse_ts(hi), row)
        for row, (_, lo, hi, _partial) in zip(rows, buckets)
    ]
    starts = [lo for lo, _, _ in edges]  # sorted: bisect finds the candidate bucket
    unbucketed = 0
    for record in records:
        when = clock.parse_ts(pluck(record, time_field)) if isinstance(record, dict) else None
        i = bisect.bisect_right(starts, when) - 1 if when is not None else -1
        if i >= 0 and when <= edges[i][1]:
            edges[i][2]["count"] += 1
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
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return float(value) == float(wanted)  # 5.0 matches "5"
        except ValueError:
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


def _by_key(where: list[tuple[str, str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for key, value in where:
        grouped.setdefault(key, []).append(value)
    return grouped


def where_summary(where: list[tuple[str, str]]) -> dict:
    """meta.where: a value per key, a list when the key was repeated (OR)."""
    return {k: v[0] if len(v) == 1 else v for k, v in _by_key(where).items()}


def filter_details(records: list, where: list[tuple[str, str]]) -> tuple[list, str]:
    """Records whose event_details match every KEY (values for the same key are
    alternatives: `--where species=buffalo --where species=elephant` is either),
    and a note that names the event types carrying no such detail at all (a
    type can say "elephant" in its name instead of in a species field; those
    cannot match and should be counted by type, not as none)."""
    grouped = _by_key(where)
    kept = []
    silent: dict[str, dict[str, int]] = {k: {} for k in grouped}
    for r in records:
        ok = True
        for key, values in grouped.items():
            results = [details_match(r, key, v) for v in values]
            if all(m is None for m in results):
                t = str(r.get("event_type") or "?") if isinstance(r, dict) else "?"
                silent[key][t] = silent[key].get(t, 0) + 1
                ok = False
            elif not any(results):
                ok = False
        if ok:
            kept.append(r)
    spec = ", ".join(f"{k}={'|'.join(v)}" for k, v in grouped.items())
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
