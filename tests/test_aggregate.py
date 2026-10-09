from zoneinfo import ZoneInfo

import click
import pytest

from earthranger_cli import aggregate


def test_group_counts_sorts_commonest_first_then_by_value():
    recs = [{"p": "red"}, {"p": "amber"}, {"p": "red"}, {"q": 1}, {"p": {"n": "x"}}]
    rows, matched = aggregate.group_counts(recs, "p")
    assert rows == [
        {"group": "red", "count": 2},
        {"group": "(missing)", "count": 1},  # {"q": 1} has no p at all
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
    rows, _ = aggregate.group_by_period(
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
    rows, _ = aggregate.group_by_period(
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
            "until": "2026-10-31T00:00:00+00:00",  # clipped to the window's --until
            "count": 1,
            "partial": True,
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
    rows, _ = aggregate.group_by_period(
        recs,
        period="day",
        since="2026-10-08T00:00:00+03:00",
        until="2026-10-09T23:59:59.999999+03:00",
        tz=tz,
        time_field="time",
    )
    assert [(r["period"], r["count"]) for r in rows] == [("2026-10-08", 1), ("2026-10-09", 1)]


def test_group_counts_present_counts_the_key_not_its_emptiness():
    rows, present = aggregate.group_counts([{"p": None}, {"p": None}, {"q": 1}], "p")
    assert rows == [{"group": "(none)", "count": 2}, {"group": "(missing)", "count": 1}]
    assert present == 2
    _, present = aggregate.group_counts([{"a": {"b": None}}, {"a": {}}], "a.b")
    assert present == 1


def test_group_by_period_reports_records_outside_the_buckets():
    tz = ZoneInfo("Africa/Nairobi")
    recs = [
        {"time": "2026-09-29T10:00:00+03:00"},  # a patrol that started before --since
        {"time": "2026-10-02T10:00:00+03:00"},
        {"time": None},
    ]
    rows, unbucketed = aggregate.group_by_period(
        recs,
        period="day",
        since="2026-10-01T00:00:00+03:00",
        until="2026-10-02T23:59:59.999999+03:00",
        tz=tz,
        time_field="time",
    )
    assert [(r["period"], r["count"]) for r in rows] == [("2026-10-01", 0), ("2026-10-02", 1)]
    assert unbucketed == 2


def test_where_same_key_values_are_ored():
    recs = [
        {"event_type": "c", "event_details": {"species": "buffalo"}},
        {"event_type": "c", "event_details": {"species": "elephant"}},
        {"event_type": "c", "event_details": {"species": "lion"}},
    ]
    kept, note = aggregate.filter_details(recs, [("species", "buffalo"), ("species", "elephant")])
    assert [r["event_details"]["species"] for r in kept] == ["buffalo", "elephant"]
    assert "species=buffalo|elephant" in note
    assert aggregate.where_summary(
        [("species", "buffalo"), ("species", "elephant"), ("cause", "x")]
    ) == {
        "species": ["buffalo", "elephant"],
        "cause": "x",
    }


def test_group_by_period_clips_partial_periods_to_the_window():
    tz = ZoneInfo("Africa/Nairobi")
    rows, _ = aggregate.group_by_period(
        [{"time": "2026-10-05T10:00:00+03:00"}],
        period="month",
        since="2026-10-03T00:00:00+03:00",
        until="2026-10-09T12:00:00+03:00",
        tz=tz,
        time_field="time",
    )
    assert rows == [
        {
            "period": "2026-10",
            "since": "2026-10-03T00:00:00+03:00",
            "until": "2026-10-09T12:00:00+03:00",
            "count": 1,
            "partial": True,
        }
    ]
    rows, _ = aggregate.group_by_period(
        [],
        period="day",
        since="2026-10-08T00:00:00+03:00",
        until="2026-10-08T23:59:59.999999+03:00",
        tz=tz,
        time_field="time",
    )
    assert rows[0]["partial"] is False


def test_where_matches_numbers_numerically():
    rec = {"event_details": {"count": 5.0, "code": "007"}}
    assert aggregate.details_match(rec, "count", "5") is True
    assert aggregate.details_match(rec, "count", "5.0") is True
    assert aggregate.details_match(rec, "count", "6") is False
    assert aggregate.details_match(rec, "code", "007") is True  # strings still compare as text
