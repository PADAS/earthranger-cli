"""The site's clock: server UTC time, site timezone, and the windows built from them.

"Today" is a question about the site's calendar, not the caller's. A site on
America/Los_Angeles rolls over seven hours after UTC does, so a UTC-day window
reports yesterday evening as today and misses everything after 17:00 local.
Everything here is computed once per invocation from one GET /status.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# das's `server_timezone` is `timezone.localtime().strftime("%Z")`: an abbreviation
# ("EAT", "PDT") or, for zones tzdb gives none, a bare offset ("+03", "+0545").
# Only the offset form is usable without a tz database; `server_timezone_name`
# is the IANA name and the first choice.
_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})?$")
_DURATION_RE = re.compile(r"^(\d+)([mhdw])$")
_UNIT = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
PERIODS = ("day", "week", "month")


def site_tz(info: dict):
    """A tzinfo for the site, or None when it reported nothing usable — and in
    that case the caller must refuse rather than silently fall back to UTC."""
    name = info.get("timezone_name")
    if name:
        try:
            return ZoneInfo(str(name))
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    m = _OFFSET_RE.match(str(info.get("timezone") or "").strip())
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        return timezone(-delta if m.group(1) == "-" else delta)
    return None


def parse_duration(raw: str) -> timedelta:
    """`30m`, `50h`, `7d`, `2w` -> a timedelta. Strict: `7` and `7x` are refused."""
    m = _DURATION_RE.match((raw or "").strip())
    if not m:
        raise ValueError(f"--last takes a count and a unit — 30m, 50h, 7d, 2w — not {raw!r}")
    return timedelta(**{_UNIT[m.group(2)]: int(m.group(1))})


def parse_ts(value) -> datetime | None:
    """ISO-8601 with Z or an offset; a naive timestamp is read as UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def last_bounds(info: dict, span: timedelta) -> tuple[str, str] | None:
    """[now − span, now] in site time. Subtract in UTC, then convert: local
    arithmetic is wall-clock arithmetic, and a window across a DST change
    came out an hour long or short."""
    tz, now = site_tz(info), parse_ts(info.get("utc"))
    if tz is None or now is None:
        return None
    return _iso((now - span).astimezone(tz)), _iso(now.astimezone(tz))


def day_bounds(info: dict, days_ago: int = 0) -> tuple[str, str] | None:
    """The site-local calendar day, ending 23:59:59 so two days never both
    claim an event filed exactly at midnight."""
    tz, now = site_tz(info), parse_ts(info.get("utc"))
    if tz is None or now is None:
        return None
    start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start -= timedelta(days=days_ago)
    return _iso(start), _iso(start + timedelta(days=1) - timedelta(seconds=1))


def fetch_clock(client) -> dict:
    """One GET /status: UTC from the HTTP Date header (the body carries no
    timestamp), the site's timezone from the body."""
    response = client._get("status", max_retries=0, return_response=True)
    date_hdr = response.headers.get("Date")
    now = parsedate_to_datetime(date_hdr).astimezone(UTC) if date_hdr else datetime.now(UTC)
    info: dict = {
        "utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timezone_name": None,
        "timezone": None,
        "local": None,
        "today": None,
    }
    try:
        body = json.loads(response.text)
    except ValueError:
        body = None
    if isinstance(body, dict) and "data" in body:
        body = body["data"]
    if isinstance(body, dict):
        info["timezone_name"] = body.get("server_timezone_name")
        info["timezone"] = body.get("server_timezone")
    tz = site_tz(info)
    if tz is not None:
        info["local"] = _iso(now.astimezone(tz))
        since, until = day_bounds(info)
        info["today"] = {"since": since, "until": until}
    return info


def clock_meta(info: dict) -> dict:
    """The three meta fields every records document carries; None values dropped."""
    meta = {
        "server_utc": info.get("utc"),
        "site_now": info.get("local"),
        "site_tz": info.get("timezone_name") or info.get("timezone"),
    }
    return {k: v for k, v in meta.items() if v}


def bucket_window(since: str, until: str, period: str, tz) -> list[tuple[str, str, str]]:
    """Calendar buckets covering [since, until] in site time: (label, lo, hi)
    with hi = next start − 1 s. Days label YYYY-MM-DD, weeks their Monday,
    months YYYY-MM."""
    if period not in PERIODS:
        raise ValueError(f"period must be one of {', '.join(PERIODS)}, not {period!r}")
    lo, hi = parse_ts(since), parse_ts(until)
    if lo is None or hi is None:
        raise ValueError("--since/--until must be ISO-8601 timestamps to bucket by period")
    cur = lo.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end = hi.astimezone(tz)
    if period == "week":
        cur -= timedelta(days=cur.weekday())
    elif period == "month":
        cur = cur.replace(day=1)
    buckets = []
    while cur <= end:
        if period == "day":
            nxt, label = cur + timedelta(days=1), cur.strftime("%Y-%m-%d")
        elif period == "week":
            nxt, label = cur + timedelta(days=7), cur.strftime("%Y-%m-%d")
        else:
            nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
            label = cur.strftime("%Y-%m")
        buckets.append((label, _iso(cur), _iso(nxt - timedelta(seconds=1))))
        cur = nxt
    return buckets
