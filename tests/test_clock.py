from datetime import UTC, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import FakeER
from earthranger_cli import clock


def test_site_tz_prefers_iana_name_then_offset_then_none():
    assert clock.site_tz({"timezone_name": "Africa/Nairobi"}) == ZoneInfo("Africa/Nairobi")
    assert clock.site_tz({"timezone_name": None, "timezone": "+03"}) == timezone(timedelta(hours=3))
    assert clock.site_tz({"timezone": "-0545"}) == timezone(-timedelta(hours=5, minutes=45))
    # Review Focus 3: an abbreviation alone is not a timezone
    assert clock.site_tz({"timezone_name": None, "timezone": "EAT"}) is None
    assert clock.site_tz({}) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("30m", timedelta(minutes=30)),
        ("50h", timedelta(hours=50)),
        ("7d", timedelta(days=7)),
        ("2w", timedelta(weeks=2)),
    ],
)
def test_parse_duration(raw, expected):
    assert clock.parse_duration(raw) == expected


@pytest.mark.parametrize("raw", ["7", "7x", "", "d7", "1.5h"])
def test_parse_duration_refuses_other_forms(raw):
    with pytest.raises(ValueError, match="count and a unit"):
        clock.parse_duration(raw)


def test_day_bounds_are_the_site_calendar_day():
    info = {"utc": "2026-10-09T02:30:00Z", "timezone_name": "America/Los_Angeles"}
    assert clock.day_bounds(info) == ("2026-10-08T00:00:00-07:00", "2026-10-08T23:59:59-07:00")
    assert clock.day_bounds(info, 1) == (
        "2026-10-07T00:00:00-07:00",
        "2026-10-07T23:59:59-07:00",
    )
    assert clock.day_bounds({"utc": "2026-10-09T02:30:00Z", "timezone": "EAT"}) is None


def test_last_bounds_subtracts_in_utc_across_dst():
    # Review Focus 2: US fall-back is 2026-11-01 09:00Z; the 24 h before
    # 2026-11-01T20:00Z span a 25-hour local gap
    info = {"utc": "2026-11-01T20:00:00Z", "timezone_name": "America/Los_Angeles"}
    since, until = clock.last_bounds(info, timedelta(hours=24))
    assert until == "2026-11-01T12:00:00-08:00"
    assert since == "2026-10-31T13:00:00-07:00"


def test_fetch_clock_reads_date_header_and_status_body():
    fake = FakeER()
    info = clock.fetch_clock(fake)
    assert info["utc"] == "2026-10-09T09:00:00Z"
    assert info["timezone_name"] == "Africa/Nairobi"
    assert info["local"] == "2026-10-09T12:00:00+03:00"
    assert info["today"] == {
        "since": "2026-10-09T00:00:00+03:00",
        "until": "2026-10-09T23:59:59+03:00",
    }
    assert fake.calls == [("_get_response", "status", None)]
    assert clock.clock_meta(info) == {
        "server_utc": "2026-10-09T09:00:00Z",
        "site_now": "2026-10-09T12:00:00+03:00",
        "site_tz": "Africa/Nairobi",
    }


def test_fetch_clock_without_usable_timezone_still_has_utc():
    fake = FakeER()
    fake.status = {"server_timezone": "EAT"}
    info = clock.fetch_clock(fake)
    assert info["utc"] == "2026-10-09T09:00:00Z"
    assert info["local"] is None and info["today"] is None
    assert clock.clock_meta(info) == {"server_utc": "2026-10-09T09:00:00Z", "site_tz": "EAT"}


def test_bucket_window_days_weeks_months():
    tz = ZoneInfo("Africa/Nairobi")
    days = clock.bucket_window("2026-10-07T10:00:00+03:00", "2026-10-09T12:00:00+03:00", "day", tz)
    assert [b[0] for b in days] == ["2026-10-07", "2026-10-08", "2026-10-09"]
    assert days[0][1:] == ("2026-10-07T00:00:00+03:00", "2026-10-07T23:59:59+03:00")
    weeks = clock.bucket_window(
        "2026-10-07T00:00:00+03:00", "2026-10-13T00:00:00+03:00", "week", tz
    )
    assert [b[0] for b in weeks] == ["2026-10-05", "2026-10-12"]  # Monday-start weeks
    months = clock.bucket_window(
        "2026-08-20T00:00:00+03:00", "2026-10-01T00:00:00+03:00", "month", tz
    )
    assert [b[0] for b in months] == ["2026-08", "2026-09", "2026-10"]
    assert months[1][1:] == ("2026-09-01T00:00:00+03:00", "2026-09-30T23:59:59+03:00")
    with pytest.raises(ValueError, match="period"):
        clock.bucket_window(
            "2026-10-07T00:00:00+03:00", "2026-10-08T00:00:00+03:00", "fortnight", tz
        )


def test_parse_ts_accepts_z_offset_and_naive():
    assert clock.parse_ts("2026-10-09T09:00:00Z").utcoffset() == timedelta(0)
    assert clock.parse_ts("2026-10-09T12:00:00+03:00").hour == 12
    assert clock.parse_ts("2026-10-09T09:00:00").tzinfo == UTC
    assert clock.parse_ts(None) is None and clock.parse_ts("garbage") is None
