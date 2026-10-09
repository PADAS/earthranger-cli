"""Time windows for the read commands, in the site's calendar.

`--since/--until` are the caller's exact bounds; `--today`, `--yesterday` and
`--last 7d` are computed from the site clock (clock.py) so an agent never does
timezone arithmetic by hand. Each endpoint spells the window differently:
events and patrols take it inside the `filter` JSON, observations and tracks
as `since`/`until`, the observations export as `after_date`/`before_date`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import click

from . import clock

_BARE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KINDS = ("filter", "since_until", "after_before")


@dataclass(frozen=True)
class WindowRequest:
    since: str | None
    until: str | None
    mode: str | None  # None | "today" | "yesterday" | "last"
    span: timedelta | None  # for "last"


def window_options(f):
    f = click.option(
        "--last",
        metavar="DURATION",
        help="The last 30m / 50h / 7d / 2w, ending now, in site time.",
    )(f)
    f = click.option("--yesterday", is_flag=True, help="The site's previous calendar day.")(f)
    f = click.option(
        "--today",
        is_flag=True,
        help="The site's current calendar day (its timezone, not UTC).",
    )(f)
    f = click.option("--until", help="ISO-8601 end; a bare date means the end of that day.")(f)
    f = click.option("--since", help="ISO-8601 start.")(f)
    return f


def parse_window(kwargs: dict) -> WindowRequest:
    """Pop the five window flags out of a command's kwargs and validate them
    before anything connects: a conflict is a usage error, not a login."""
    since, until = kwargs.pop("since", None), kwargs.pop("until", None)
    today = kwargs.pop("today", False)
    yesterday = kwargs.pop("yesterday", False)
    last = kwargs.pop("last", None)
    chosen = [n for n, v in (("--today", today), ("--yesterday", yesterday), ("--last", last)) if v]
    if len(chosen) > 1:
        raise click.UsageError("pass only one of --today, --yesterday, --last.")
    if chosen and (since or until):
        clash = "--since" if since else "--until"
        raise click.UsageError(f"{chosen[0]} sets the whole window; drop {clash}.")
    span = None
    if last:
        try:
            span = clock.parse_duration(last)
        except ValueError as e:
            raise click.UsageError(str(e)) from None
    until = inclusive_until(until)
    for flag, value in (("--since", since), ("--until", until)):
        if value is not None and clock.parse_ts(value) is None:
            raise click.UsageError(f"{flag} must be an ISO-8601 timestamp, got {value!r}.")
    mode = chosen[0][2:] if chosen else None
    return WindowRequest(since, until, mode, span)


def inclusive_until(until: str | None) -> str | None:
    """ER reads `2026-08-31` as midnight *opening* the 31st, so --until on a bare
    date left the whole day out. Anything with a time in it is left alone."""
    if until and _BARE_DATE.match(until.strip()):
        return f"{until.strip()}T23:59:59.999999"
    return until


def resolve_window(
    req: WindowRequest,
    *,
    get_info: Callable[[], dict] | None,
    default_window: timedelta | None,
    note: Callable[[str], None] | None,
) -> tuple[str | None, str | None, dict | None]:
    """Turn the request into concrete bounds. Returns (since, until, meta);
    meta is None when no window applies. A clock-based mode on a site with no
    usable timezone refuses (exit 1) rather than answering in UTC."""
    since, until = req.since, req.until
    meta: dict | None = None
    if req.mode:
        info = get_info()
        if req.mode == "last":
            bounds = clock.last_bounds(info, req.span)
        else:
            bounds = clock.day_bounds(info, 1 if req.mode == "yesterday" else 0)
        if bounds is None:
            reported = info.get("timezone_name") or info.get("timezone") or "none"
            raise click.ClickException(
                f"the site reported no usable timezone ({reported}); "
                "pass --since/--until explicitly."
            )
        since, until = bounds
        meta = {
            "since": since,
            "until": until,
            "tz": info.get("timezone_name") or info.get("timezone"),
            "mode": req.mode,
        }
    elif since is None and default_window is not None:
        end = clock.parse_ts(until) if until else None
        end = end or datetime.now(UTC)
        since = (end - default_window).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        hours = int(default_window.total_seconds() // 3600)
        anchor = f"--until {until}" if until else "now"
        if note:
            note(
                f"note: no --since given; defaulting to the {hours} hours before {anchor} ({since})."
            )
    if since is not None or until is not None:
        meta = meta or {"since": since, "until": until}
    return since, until, meta


def load_filter(params: dict) -> dict:
    """The `filter` query param as a dict (empty when absent); a usage error
    when it is not a JSON object, since other flags merge keys into it."""
    raw = params.get("filter")
    try:
        current = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        raise click.UsageError(f"--filter is not valid JSON ({e}).") from None
    if not isinstance(current, dict):
        raise click.UsageError("--filter must be a JSON object.")
    return current


def merge_filter(params: dict, **fields) -> None:
    """Set keys inside the `filter` JSON object, keeping whatever else it holds."""
    current = load_filter(params)
    current.update(fields)
    params["filter"] = json.dumps(current, separators=(",", ":"))


def apply_window(kind: str, params: dict, since, until) -> None:
    """Write the bounds into the request the way this endpoint spells them."""
    if since is None and until is None:
        return
    if kind == "filter":
        window = dict(load_filter(params).get("date_range") or {})
        if since is not None:
            window["lower"] = since
        if until is not None:
            window["upper"] = until
        merge_filter(params, date_range=window)
        return
    lower, upper = ("since", "until") if kind == "since_until" else ("after_date", "before_date")
    if since is not None:
        params[lower] = _aware(since)
    if until is not None:
        params[upper] = _aware(until)


def _aware(value: str) -> str:
    """A naive bound with an explicit UTC offset. das reads a naive observation
    bound as UTC on the search endpoint and chokes comparing one against an
    aware datetime on the export, so the CLI never sends a naive wall time to
    these endpoints; an aware value passes through untouched."""
    parsed = clock.parse_ts(value)
    if parsed is None or value.strip()[-1] in "Zz" or re.search(r"[+-]\d{2}:?\d{2}$", value):
        return value
    return parsed.isoformat(timespec="microseconds" if parsed.microsecond else "seconds")
