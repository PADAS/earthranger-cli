from zoneinfo import ZoneInfo

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
        until="2026-10-09T23:59:59+03:00",
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
            "until": "2026-10-31T23:59:59+00:00",
            "count": 1,
        }
    ]
