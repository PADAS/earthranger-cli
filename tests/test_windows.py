import json
from datetime import timedelta

import click
import pytest

from earthranger_cli import windows

CLOCK = {"utc": "2026-10-09T09:00:00Z", "timezone_name": "Africa/Nairobi", "timezone": "EAT"}


def _kw(**overrides):
    kw = {"since": None, "until": None, "today": False, "yesterday": False, "last": None}
    kw.update(overrides)
    return kw


def test_parse_window_pops_flags_and_rejects_conflicts():
    kw = _kw(since="2026-10-01", x=1)
    req = windows.parse_window(kw)
    assert (req.since, req.until, req.mode) == ("2026-10-01", None, None)
    assert kw == {"x": 1}
    with pytest.raises(click.UsageError, match="only one of --today, --yesterday, --last"):
        windows.parse_window(_kw(today=True, yesterday=True))
    with pytest.raises(click.UsageError, match="--last sets the whole window; drop --since"):
        windows.parse_window(_kw(since="2026-10-01", last="7d"))
    with pytest.raises(click.UsageError, match="count and a unit"):
        windows.parse_window(_kw(last="7"))


def test_inclusive_until_extends_a_bare_date_only():
    assert windows.inclusive_until("2026-08-31") == "2026-08-31T23:59:59.999999"  # Review Focus 1
    assert windows.inclusive_until("2026-08-31T10:00:00Z") == "2026-08-31T10:00:00Z"
    assert windows.inclusive_until(None) is None


def test_resolve_window_today_and_last_use_the_clock():
    req = windows.WindowRequest(None, None, "today", None)
    since, until, meta = windows.resolve_window(
        req, get_info=lambda: CLOCK, default_window=None, note=None
    )
    assert (since, until) == ("2026-10-09T00:00:00+03:00", "2026-10-09T23:59:59.999999+03:00")
    assert meta == {"since": since, "until": until, "tz": "Africa/Nairobi", "mode": "today"}
    req = windows.WindowRequest(None, None, "last", timedelta(hours=2))
    since, until, meta = windows.resolve_window(
        req, get_info=lambda: CLOCK, default_window=None, note=None
    )
    assert (since, until) == ("2026-10-09T10:00:00+03:00", "2026-10-09T12:00:00+03:00")
    assert meta["mode"] == "last"


def test_resolve_window_refuses_without_a_timezone():
    req = windows.WindowRequest(None, None, "today", None)
    with pytest.raises(click.ClickException, match="no usable timezone"):
        windows.resolve_window(
            req,
            get_info=lambda: {"utc": "2026-10-09T09:00:00Z", "timezone": "EAT"},
            default_window=None,
            note=None,
        )


def test_resolve_window_default_fills_since_and_notes(capsys):
    req = windows.WindowRequest(None, "2026-10-09T12:00:00Z", None, None)
    since, until, meta = windows.resolve_window(
        req,
        get_info=lambda: CLOCK,
        default_window=timedelta(hours=24),
        note=lambda s: click.echo(s, err=True),
    )
    assert since == "2026-10-08T12:00:00Z" and until == "2026-10-09T12:00:00Z"
    assert (
        "defaulting to the 24 hours before --until 2026-10-09T12:00:00Z" in capsys.readouterr().err
    )
    assert meta == {"since": since, "until": until}


def test_resolve_window_explicit_passthrough_has_meta_without_tz():
    req = windows.WindowRequest("2026-10-01", "2026-10-02T00:00:00Z", None, None)
    assert windows.resolve_window(req, get_info=lambda: CLOCK, default_window=None, note=None) == (
        "2026-10-01",
        "2026-10-02T00:00:00Z",
        {"since": "2026-10-01", "until": "2026-10-02T00:00:00Z"},
    )
    empty = windows.WindowRequest(None, None, None, None)
    assert windows.resolve_window(empty, get_info=None, default_window=None, note=None) == (
        None,
        None,
        None,
    )


def test_apply_window_folds_into_filter_and_merges():
    params = {"filter": json.dumps({"text": "lion", "date_range": {"lower": "old"}})}
    windows.apply_window("filter", params, "2026-10-01", None)
    assert json.loads(params["filter"]) == {"text": "lion", "date_range": {"lower": "2026-10-01"}}
    params = {}
    windows.apply_window("filter", params, None, "2026-10-02")
    assert json.loads(params["filter"]) == {"date_range": {"upper": "2026-10-02"}}
    with pytest.raises(click.UsageError, match="--filter is not valid JSON"):
        windows.apply_window("filter", {"filter": "{nope"}, "2026-10-01", None)
    params = {}
    windows.apply_window("since_until", params, "a", "b")
    assert params == {"since": "a", "until": "b"}
    params = {}
    windows.apply_window("after_before", params, "a", None)
    assert params == {"after_date": "a"}


def test_parse_window_rejects_unparseable_bounds_before_connecting():
    with pytest.raises(click.UsageError, match="--until must be an ISO-8601 timestamp"):
        windows.parse_window(_kw(until="yesterday-ish"))
    with pytest.raises(click.UsageError, match="--since must be an ISO-8601 timestamp"):
        windows.parse_window(_kw(since="01/10/2026"))
    assert windows.parse_window(_kw(since="2026-10-01")).since == "2026-10-01"
