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
    assert clock.day_bounds(info) == (
        "2026-10-08T00:00:00-07:00",
        "2026-10-08T23:59:59.999999-07:00",
    )
    assert clock.day_bounds(info, 1) == (
        "2026-10-07T00:00:00-07:00",
        "2026-10-07T23:59:59.999999-07:00",
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
        "until": "2026-10-09T23:59:59.999999+03:00",
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
    assert days[0][1:] == ("2026-10-07T00:00:00+03:00", "2026-10-07T23:59:59.999999+03:00")
    weeks = clock.bucket_window(
        "2026-10-07T00:00:00+03:00", "2026-10-13T00:00:00+03:00", "week", tz
    )
    assert [b[0] for b in weeks] == ["2026-10-05", "2026-10-12"]  # Monday-start weeks
    months = clock.bucket_window(
        "2026-08-20T00:00:00+03:00", "2026-10-01T00:00:00+03:00", "month", tz
    )
    assert [b[0] for b in months] == ["2026-08", "2026-09", "2026-10"]
    assert months[1][1:] == ("2026-09-01T00:00:00+03:00", "2026-09-30T23:59:59.999999+03:00")
    with pytest.raises(ValueError, match="period"):
        clock.bucket_window(
            "2026-10-07T00:00:00+03:00", "2026-10-08T00:00:00+03:00", "fortnight", tz
        )


def test_parse_ts_accepts_z_offset_and_naive():
    assert clock.parse_ts("2026-10-09T09:00:00Z").utcoffset() == timedelta(0)
    assert clock.parse_ts("2026-10-09T12:00:00+03:00").hour == 12
    assert clock.parse_ts("2026-10-09T09:00:00").tzinfo == UTC
    assert clock.parse_ts(None) is None and clock.parse_ts("garbage") is None


def test_bucket_window_reads_naive_bounds_in_site_time():
    # review finding: a bare --until (naive, end of day) read as UTC spilled into
    # a phantom next month on a +03:00 site
    tz = ZoneInfo("Africa/Nairobi")
    months = clock.bucket_window("2026-07-01", "2026-09-30T23:59:59.999999", "month", tz)
    assert [b[0] for b in months] == ["2026-07", "2026-08", "2026-09"]
    days = clock.bucket_window("2026-10-08", "2026-10-08T23:59:59.999999", "day", tz)
    assert [b[0] for b in days] == ["2026-10-08"]


def test_bucket_window_naive_tz_follows_the_endpoint():
    # review: das parses naive observation bounds as UTC (observations.utils.dateparse),
    # so a UTC day on a +03:00 site spans two site-local days
    from datetime import UTC

    tz = ZoneInfo("Africa/Nairobi")
    utc_day = clock.bucket_window(
        "2026-10-08", "2026-10-08T23:59:59.999999", "day", tz, naive_tz=UTC
    )
    assert [b[0] for b in utc_day] == ["2026-10-08", "2026-10-09"]
    site_day = clock.bucket_window("2026-10-08", "2026-10-08T23:59:59.999999", "day", tz)
    assert [b[0] for b in site_day] == ["2026-10-08"]


def test_period_ends_keep_the_final_fractional_second():
    # review: a one-second gap between buckets dropped records stamped in the last 999 ms
    info = {"utc": "2026-10-09T02:30:00Z", "timezone_name": "America/Los_Angeles"}
    assert clock.day_bounds(info)[1] == "2026-10-08T23:59:59.999999-07:00"
    tz = ZoneInfo("Africa/Nairobi")
    (_label, lo, hi), *_ = clock.bucket_window("2026-10-08", "2026-10-08", "day", tz)
    assert (lo, hi) == ("2026-10-08T00:00:00+03:00", "2026-10-08T23:59:59.999999+03:00")


def test_fetch_clock_survives_a_malformed_date_header():
    from conftest import FakeResponse

    fake = FakeER()
    fake.responses["status"] = FakeResponse(fake.status, date="not a date at all")
    info = clock.fetch_clock(fake)
    assert info["utc"].endswith("Z") and info["timezone_name"] == "Africa/Nairobi"


def test_fetch_clock_reads_an_unknown_local_date_header_as_utc():
    # review: '-0000' makes parsedate_to_datetime return a naive datetime, which
    # astimezone would read as the host's local time
    from conftest import FakeResponse

    fake = FakeER()
    fake.responses["status"] = FakeResponse(fake.status, date="Tue, 06 Oct 2026 10:00:00 -0000")
    assert clock.fetch_clock(fake)["utc"] == "2026-10-06T10:00:00Z"
