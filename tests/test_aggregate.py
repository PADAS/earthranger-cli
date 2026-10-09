from zoneinfo import ZoneInfo

import click
import pytest

from earthranger_cli import aggregate


def test_group_counts_sorts_commonest_first_then_by_value():
    recs = [{"p": "red"}, {"p": "amber"}, {"p": "red"}, {"q": 1}, {"p": {"n": "x"}}]
    rows, matched = aggregate.group_counts(recs, "p")
    assert rows == [
        {"group": "red", "count": 2},
        {"group": "(none)", "count": 1},
        {"group": "amber", "count": 1},
        {"group": '{"n":"x"}', "count": 1},
    ]
    assert matched == 4


def test_scalar_keys_lists_groupable_fields():
    assert aggregate.scalar_keys([{"a": 1, "b": [1], "c": "x"}, {"d": None}]) == ["a", "c", "d"]


def test_group_by_period_counts_into_site_buckets():
    tz = ZoneInfo("Africa/Nairobi")
    recs = [
        {"time": "2026-10-07T21:30:00Z"},  # 00:30 on the 8th in Nairobi
        {"time": "2026-10-08T05:00:00Z"},
        {"time": "2026-10-09T05:00:00Z"},
        {"time": None},
    ]
    rows = aggregate.group_by_period(
        recs,
        period="day",
        since="2026-10-07T00:00:00+03:00",
        until="2026-10-09T23:59:59.999999+03:00",
        tz=tz,
        time_field="time",
    )
    assert [(r["period"], r["count"]) for r in rows] == [
        ("2026-10-07", 0),
        ("2026-10-08", 2),
        ("2026-10-09", 1),
    ]
    assert rows[1]["since"] == "2026-10-08T00:00:00+03:00"


def test_group_by_period_uses_dotted_time_field():
    tz = ZoneInfo("UTC")
    recs = [{"patrol_segments": [{"time_range": {"start_time": "2026-10-08T05:00:00Z"}}]}]
    rows = aggregate.group_by_period(
        recs,
        period="month",
        since="2026-10-01T00:00:00Z",
        until="2026-10-31T00:00:00Z",
        tz=tz,
        time_field="patrol_segments.0.time_range.start_time",
    )
    assert rows == [
        {
            "period": "2026-10",
            "since": "2026-10-01T00:00:00+00:00",
            "until": "2026-10-31T23:59:59.999999+00:00",
            "count": 1,
        }
    ]


def test_parse_where_and_details_match():
    assert aggregate.parse_where(("species=Buffalo", " cause = poached ")) == [
        ("species", "Buffalo"),
        ("cause", "poached"),
    ]
    with pytest.raises(click.UsageError, match="KEY=VALUE"):
        aggregate.parse_where(("species",))
    rec = {
        "event_details": {
            "species": "buffalo",
            "tags": ["a", "B"],
            "who": {"name": "Ann", "value": "ann"},
        }
    }
    assert aggregate.details_match(rec, "species", "BUFFALO") is True
    assert aggregate.details_match(rec, "tags", "b") is True
    assert aggregate.details_match(rec, "who", "ann") is True
    assert aggregate.details_match(rec, "species", "lion") is False
    assert aggregate.details_match(rec, "cause", "x") is None
    assert aggregate.details_match({"event_details": None}, "species", "x") is None


def test_filter_details_keeps_matches_and_names_silent_types():
    recs = [
        {"event_type": "carcass", "event_details": {"species": "buffalo"}},
        {"event_type": "carcass", "event_details": {"species": "lion"}},
        {"event_type": "elephant_carcass", "event_details": {}},
    ]
    kept, note = aggregate.filter_details(recs, [("species", "buffalo")])
    assert [r["event_details"]["species"] for r in kept] == ["buffalo"]
    assert "1 of 3 event(s) matched" in note
    assert "1 event(s) have no 'species' detail" in note and "elephant_carcass (1)" in note


def test_group_by_period_counts_the_last_fractional_second_of_a_day():
    tz = ZoneInfo("Africa/Nairobi")
    recs = [{"time": "2026-10-08T23:59:59.500000+03:00"}, {"time": "2026-10-09T00:00:00+03:00"}]
    rows = aggregate.group_by_period(
        recs,
        period="day",
        since="2026-10-08T00:00:00+03:00",
        until="2026-10-09T23:59:59.999999+03:00",
        tz=tz,
        time_field="time",
    )
    assert [(r["period"], r["count"]) for r in rows] == [("2026-10-08", 1), ("2026-10-09", 1)]
