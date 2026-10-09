"""Counting and grouping records in the CLI rather than in the model's head.

The breakdown question ("how many of each priority", "events per month") has
no server-side answer in ER, so the choice is between the CLI counting and the
caller counting; a caller adding up a table is where sums go wrong.
"""

from __future__ import annotations

from . import clock
from .output import cell, pluck


def group_counts(records: list, field: str) -> tuple[list[dict], int]:
    """Count records per distinct value of `field`. Returns (rows, matched):
    `matched` is how many records carried the field at all, so a caller can
    tell "everything is None" from "there were no records". Commonest first,
    then by value, because that is the order the answer is read in."""
    counts: dict[str, int] = {}
    matched = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        value = pluck(record, field)
        if value is not None and value != "":
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
    records: list, *, period: str, since: str, until: str, tz, time_field: str
) -> list[dict]:
    """Count records into site-local day/week/month buckets over [since, until],
    reading each record's timestamp from the dotted `time_field`."""
    buckets = clock.bucket_window(since, until, period, tz)
    rows = [{"period": label, "since": lo, "until": hi, "count": 0} for label, lo, hi in buckets]
    edges = [
        (clock.parse_ts(lo), clock.parse_ts(hi), row) for row, (_, lo, hi) in zip(rows, buckets)
    ]
    for record in records:
        when = clock.parse_ts(pluck(record, time_field)) if isinstance(record, dict) else None
        if when is None:
            continue
        for lo, hi, row in edges:
            if lo <= when <= hi:
                row["count"] += 1
                break
    return rows
