import json

import click
import pytest
from click.testing import CliRunner

import earthranger_cli.cli as cli_mod
from conftest import FakeER
from earthranger_cli.cli import main
from earthranger_cli.read_commands import COMMANDS


@pytest.fixture
def fake(monkeypatch):
    fake = FakeER()
    monkeypatch.setattr(cli_mod, "_connect", lambda ctx: fake)
    return fake


def _run(args):
    return CliRunner().invoke(main, args, catch_exceptions=False)


def _gets(fake):
    return [c for c in fake.calls if c[0] == "_get"]


def test_every_command_is_registered_with_output_option():
    for spec in COMMANDS:
        group = main.commands[spec.group]
        cmd = group.commands[spec.name]
        names = {p.name for p in cmd.params}
        assert "output" in names, (spec.group, spec.name)
        assert ("limit" in names) == (spec.kind == "list"), (spec.group, spec.name)
        paginated = spec.kind == "list" and spec.unwrap is None
        assert ("page_size" in names) == paginated, (spec.group, spec.name)
        assert ("since" in names) == (spec.window is not None), (spec.group, spec.name)
        assert ("today" in names) == (spec.window is not None), (spec.group, spec.name)
        assert cmd.help and spec.help in cmd.help
        assert ("{id}" in spec.path) == (spec.arg is not None), (spec.group, spec.name)


def test_every_paginated_command_answers_to_list_and_search():
    for spec in COMMANDS:
        if spec.kind != "list" or spec.group == "events":
            continue
        group = main.commands[spec.group]
        assert group.commands["list"] is group.commands["search"], spec.group
        assert group.commands[spec.name] is group.commands["list"], spec.group
    # get-only groups gain nothing
    assert set(main.commands["tracks"].commands) == {"get"}
    # `events list` is the authoring sub-group (categories, event-types) and
    # must not be shadowed by an alias of `events search`
    events = main.commands["events"].commands
    assert isinstance(events["list"], click.Group)
    assert {"categories", "event-types"} <= set(events["list"].commands)
    assert events["search"] is not events["list"]


def test_list_alias_runs_the_same_command(fake):
    fake.responses["subjects"] = {"count": 0, "next": None, "results": []}
    result = _run(["subjects", "list"])
    assert result.exit_code == 0, result.output
    assert _gets(fake)[0][1] == "subjects"
    fake.responses["regions"] = {"count": 0, "next": None, "results": []}
    result = _run(["regions", "search"])
    assert result.exit_code == 0, result.output
    assert _gets(fake)[1][1] == "regions"


def test_spatial_feature_groups_and_features_replace_fences(fake):
    assert "fences" not in main.commands
    assert set(main.commands["spatial-feature-groups"].commands) == {"list", "search", "get"}
    assert set(main.commands["spatial-features"].commands) == {"list", "search", "get"}

    fake.responses["spatialfeaturegroup"] = {"count": 0, "next": None, "results": []}
    result = _run(["spatial-feature-groups", "list", "--sort-by", "-updated_at"])
    assert result.exit_code == 0, result.output
    assert _gets(fake)[0][1:3] == (
        "spatialfeaturegroup",
        {"sort_by": "-updated_at", "page_size": 100},
    )

    fake.responses["spatialfeaturegroup/g-1"] = {"id": "g-1", "name": "Roads"}
    result = _run(["spatial-feature-groups", "get", "g-1"])
    assert json.loads(result.output)["records"] == [{"id": "g-1", "name": "Roads"}]

    # das's SpatialFeatureFilterSet uses CSVWidget: one comma-joined value, not repeats
    fake.responses["spatialfeature"] = {"count": 0, "next": None, "results": []}
    result = _run(["spatial-features", "list", "--feature-class", "t-1,t-2"])
    assert result.exit_code == 0, result.output
    assert _gets(fake)[2][2] == {"feature_class": "t-1,t-2", "page_size": 100}

    fake.responses["spatialfeature/f-1"] = {"type": "Feature", "id": "f-1"}
    result = _run(["spatial-features", "get", "f-1"])
    assert json.loads(result.output)["records"][0]["id"] == "f-1"


def test_featuresets_list_unwraps_the_features_envelope(fake):
    # das's FeatureSetListJsonView: {"features": [...]}, not a DRF page
    fake.responses["featureset"] = {
        "features": [
            {"id": "f1", "name": "Boundaries", "types": [], "description": "", "geojson_url": "/x"},
            {"id": "f2", "name": "Roads", "types": [], "description": "", "geojson_url": "/y"},
        ]
    }
    result = _run(["featuresets", "list", "--include-hidden"])
    assert result.exit_code == 0, result.output
    assert _gets(fake)[0][1:3] == ("featureset", {"include_hidden": "true"})  # no page_size
    doc = json.loads(result.output)
    assert [r["id"] for r in doc["records"]] == ["f1", "f2"]
    assert doc["meta"] == {"total": 2, "pages": 1, "count_reported": 2}

    result = _run(["featuresets", "list", "--limit", "1"])
    assert [r["id"] for r in json.loads(result.output)["records"]] == ["f1"]

    fake.responses["featureset"] = {"features": []}
    result = _run(["featuresets", "list"])
    assert json.loads(result.output) == {
        "records": [],
        "meta": {"total": 0, "pages": 1, "count_reported": 0},
    }


def test_featureset_get_keeps_geojson_feature_collection_whole(fake):
    # the unwrap is per-row: a FeatureCollection from `get` must stay one record
    fake.responses["featureset/f1"] = {"type": "FeatureCollection", "features": [{"id": "a"}]}
    result = _run(["featuresets", "get", "f1"])
    doc = json.loads(result.output)
    assert doc["records"][0]["type"] == "FeatureCollection"
    assert doc["meta"]["total"] == 1


def test_help_shows_the_endpoint():
    result = _run(["tracks", "get", "--help"])
    assert "[GET /api/v2.0/subject/{subject_id}/tracks]" in result.output
    result = _run(["subjects", "search", "--help"])
    assert "[GET /api/v1.0/subjects]" in result.output
    assert "--updated-since" in result.output and "--updated_since" in result.output


def test_list_paginates_and_emits_records_meta(fake):
    fake.responses["subjects"] = {
        "count": 3,
        "next": "https://fake/api/v1.0/subjects/?page=2",
        "results": [{"id": "a"}, {"id": "b"}],
    }
    fake.responses["https://fake.pamdas.org/api/v1.0/subjects/?page=2"] = {
        "count": 3,
        "next": None,
        "results": [{"id": "c"}],
    }
    result = _run(["subjects", "search", "--name", "Najin", "--render-last-location"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert [r["id"] for r in doc["records"]] == ["a", "b", "c"]
    assert doc["meta"] == {"total": 3, "pages": 2, "count_reported": 3}
    first = _gets(fake)[0]
    assert first[1] == "subjects"
    assert first[2] == {"name": "Najin", "render_last_location": "true", "page_size": 100}
    assert first[4] == 0  # max_retries


def test_underscored_alias_is_accepted(fake):
    fake.responses["subjects"] = {"count": 0, "next": None, "results": []}
    result = _run(["subjects", "search", "--updated_since", "2026-01-01T00:00:00Z"])
    assert result.exit_code == 0, result.output
    assert _gets(fake)[0][2]["updated_since"] == "2026-01-01T00:00:00Z"


def test_limit_caps_records_and_requests(fake):
    fake.responses["subjects"] = {
        "count": 5,
        "next": "https://fake/?page=2",
        "results": [{"id": "a"}, {"id": "b"}],
    }
    result = _run(["subjects", "search", "--limit", "2"])
    doc = json.loads(result.output)
    assert [r["id"] for r in doc["records"]] == ["a", "b"]
    assert doc["meta"]["pages"] == 1
    assert len(_gets(fake)) == 1


def test_get_substitutes_positional_into_path(fake):
    fake.responses["subject/s-1"] = {"id": "s-1", "name": "Najin"}
    result = _run(["subjects", "get", "s-1"])
    doc = json.loads(result.output)
    assert doc["records"] == [{"id": "s-1", "name": "Najin"}]
    assert doc["meta"] == {"total": 1, "pages": 1, "count_reported": 1}
    assert _gets(fake)[0][2] == {}  # no page_size on a get


def test_tracks_uses_v2_root_and_float_flags(fake):
    fake.responses["subject/s-1/tracks"] = {"type": "FeatureCollection", "features": []}
    result = _run(
        ["tracks", "get", "s-1", "--since", "2026-06-01T00:00:00Z", "--max-speed-kmh", "80"]
    )
    assert result.exit_code == 0, result.output
    call = _gets(fake)[0]
    assert call[3] == "https://fake.pamdas.org/api/v2.0"
    assert call[2] == {"since": "2026-06-01T00:00:00Z", "max_speed_kmh": 80.0}
    assert json.loads(result.output)["records"][0]["type"] == "FeatureCollection"


def test_events_search_lives_under_existing_events_group(fake):
    fake.responses["activity/events"] = {"count": 1, "next": None, "results": [{"id": "e1"}]}
    result = _run(
        [
            "events",
            "search",
            "--event-type",
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "--state",
            "active",
        ]
    )
    assert result.exit_code == 0, result.output
    assert _gets(fake)[0][2] == {
        "event_type": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
        "state": ["active"],
        "page_size": 100,
    }
    # the authoring commands are still there
    assert {"apply", "post", "list", "show", "pull", "search", "get"} <= set(
        main.commands["events"].commands
    )


def test_whoami_and_status(fake):
    fake.responses["status"] = {"server_time": "2026-09-23T00:00:00Z"}
    result = _run(["auth", "whoami"])
    assert json.loads(result.output)["records"] == [fake.me]
    result = _run(["status", "show"])
    assert json.loads(result.output)["records"][0]["server_time"] == "2026-09-23T00:00:00Z"


def test_output_writes_file_and_summarizes_on_stderr(fake, tmp_path):
    fake.responses["regions"] = [{"id": "r1"}, {"id": "r2"}]
    target = tmp_path / "out" / "regions.json"
    result = _run(["regions", "list", "-o", str(target)])
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert result.stderr == f"Done. 2 record(s) written to {target} (1 page(s)).\n"
    assert json.loads(target.read_text())["meta"] == {
        "total": 2,
        "pages": 1,
        "count_reported": 2,
    }


def test_api_error_is_reported_cleanly(fake):
    from erclient.er_errors import ERClientNotFound

    def boom(path, **kw):
        raise ERClientNotFound()

    fake._get = boom
    result = _run(["subjects", "get", "nope"])
    assert result.exit_code == 1
    assert result.output.startswith("error: NotFound")


def test_connection_flags_accepted_after_subcommand(fake):
    fake.responses["regions"] = []
    result = _run(["regions", "list", "--server", "sandbox", "--token", "t"])
    assert result.exit_code == 0, result.output


def test_get_positional_is_percent_encoded(fake):
    fake.responses["source/abc%2Fdef"] = {"id": "abc/def"}
    result = _run(["sources", "get", "abc/def"])
    assert result.exit_code == 0, result.output
    assert _gets(fake)[0][1] == "source/abc%2Fdef"


def _encoded(params) -> str:
    """What requests actually puts on the wire for these params."""
    from requests import PreparedRequest

    req = PreparedRequest()
    req.prepare_url("https://x.test/", params)
    return req.url.split("?", 1)[1]


def test_tracks_max_gap_minutes_is_an_integer_on_the_wire(fake):
    # das parses this with int(); "30.0" fails that and silently disables the gap
    fake.responses["subject/s-1/tracks"] = {"type": "FeatureCollection", "features": []}
    result = _run(["tracks", "get", "s-1", "--max-gap-minutes", "30"])
    assert result.exit_code == 0, result.output
    params = _gets(fake)[0][2]
    assert params == {"max_gap_minutes": 30}
    assert _encoded(params) == "max_gap_minutes=30"


def test_events_multi_value_filters_are_repeated_query_params(fake):
    # das reads state/event_type with getlist(): "a,b" as one value matches
    # the literal string "a,b"; it must go out as ?state=a&state=b
    fake.responses["activity/events"] = {"count": 0, "next": None, "results": []}
    result = _run(
        [
            "events",
            "search",
            "--state",
            "active, resolved",
            "--event-type",
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa,bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        ]
    )
    assert result.exit_code == 0, result.output
    params = _gets(fake)[0][2]
    assert params["state"] == ["active", "resolved"]
    assert params["event_type"] == [
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ]
    assert (
        _encoded({k: params[k] for k in ("state", "event_type")})
        == "state=active&state=resolved&event_type=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa&event_type=bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    )


@pytest.mark.parametrize("option", ["--limit", "--page-size", "--page_size"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_pagination_controls_reject_nonpositive_values_before_connect(monkeypatch, option, value):
    from unittest.mock import Mock

    connect = Mock()
    monkeypatch.setattr(cli_mod, "_connect", connect)
    result = _run(["subjects", "search", option, value])
    assert result.exit_code == 2
    assert "Invalid value" in result.output
    connect.assert_not_called()


def test_observations_guard_runs_before_any_connection(monkeypatch):
    # no `fake` fixture: _connect is real, and there is no server, profile or
    # password anywhere — the selector error must still be what the user sees
    monkeypatch.setattr(cli_mod, "make_client", lambda **kw: pytest.fail("must not connect"))
    result = _run(["observations", "search"])
    assert result.exit_code == 2
    assert "needs exactly one of --subject-id" in result.output
    assert "Missing server" not in result.output
    assert "Password" not in result.output

    result = _run(["observations", "search", "--source-id", "s", "--until", "nope"])
    assert result.exit_code == 2
    assert "--until must be an ISO-8601 timestamp" in result.output
    assert "Missing server" not in result.output


def test_observations_refuses_an_unbounded_request(fake):
    result = _run(["observations", "search"])
    assert result.exit_code == 2
    assert (
        "needs exactly one of --subject-id, --source-id, --subjectsource-id, --sourceprovider-id"
        in (result.output)
    )
    assert _gets(fake) == []  # nothing was requested

    result = _run(["observations", "search", "--subject-id", "s-1", "--source-id", "src-1"])
    assert result.exit_code == 2
    assert "pass only one of" in result.output and "subject_id, source_id" in result.output
    assert _gets(fake) == []


def test_observations_defaults_since_to_last_24h_and_says_so(fake):
    from datetime import UTC, datetime, timedelta

    fake.responses["observations"] = {"count": 0, "next": None, "results": []}
    runner = CliRunner()  # click >= 8.2 keeps stderr separate by default
    result = runner.invoke(
        main, ["observations", "search", "--subject-id", "s-1"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    params = _gets(fake)[0][2]
    assert params["subject_id"] == "s-1"
    since = datetime.strptime(params["since"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert (
        timedelta(hours=23, minutes=59) < datetime.now(UTC) - since < timedelta(hours=24, minutes=1)
    )
    assert "until" not in params  # ER defaults it to now
    assert "note: no --since given; defaulting to the 24 hours before now" in result.stderr
    assert json.loads(result.stdout)["records"] == []  # stdout stays pure JSON


def test_observations_explicit_since_and_provider_selector_pass_through(fake):
    fake.responses["observations"] = {"count": 0, "next": None, "results": []}
    runner = CliRunner()  # click >= 8.2 keeps stderr separate by default
    result = runner.invoke(
        main,
        ["observations", "search", "--sourceprovider-id", "p-1", "--since", "2026-01-01T00:00:00Z"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert _gets(fake)[0][2] == {
        "sourceprovider_id": "p-1",
        "since": "2026-01-01T00:00:00Z",
        "page_size": 100,
    }
    assert "defaulting" not in result.stderr


def test_observations_default_since_is_anchored_to_an_explicit_until(fake):
    fake.responses["observations"] = {"count": 0, "next": None, "results": []}
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["observations", "search", "--source-id", "src-1", "--until", "2026-01-02T00:00:00Z"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    params = _gets(fake)[0][2]
    assert params["until"] == "2026-01-02T00:00:00Z"
    assert params["since"] == "2026-01-01T00:00:00Z"  # 24h before --until, not before now
    assert "defaulting to the 24 hours before --until 2026-01-02T00:00:00Z" in result.stderr


def test_observations_rejects_unparseable_until_before_requesting(fake):
    result = _run(["observations", "search", "--source-id", "src-1", "--until", "yesterday"])
    assert result.exit_code == 2
    assert "--until must be an ISO-8601 timestamp" in result.output
    assert _gets(fake) == []


@pytest.mark.parametrize(
    ("until", "expected_since"),
    [
        ("2026-01-02T00:00:00+03:00", "2025-12-31T21:00:00Z"),  # positive offset
        ("2026-01-02T00:00:00-05:00", "2026-01-01T05:00:00Z"),  # negative offset
        ("2026-01-02T00:00:00", "2026-01-01T00:00:00Z"),  # naive: treated as UTC
    ],
)
def test_observations_default_since_converts_until_offset_to_utc(fake, until, expected_since):
    fake.responses["observations"] = {"count": 0, "next": None, "results": []}
    result = _run(["observations", "search", "--source-id", "src-1", "--until", until])
    assert result.exit_code == 0, result.output
    assert _gets(fake)[0][2]["since"] == expected_since


_TYPES = [
    {
        "id": "11111111-1111-1111-1111-111111111111",
        "value": "geofence_break",
        "display": "Geofence Break",
    },
    {"id": "22222222-2222-2222-2222-222222222222", "value": "high_speed", "display": "High Speed"},
    {
        "id": "33333333-3333-3333-3333-333333333333",
        "value": "old_type",
        "display": "Old",
        "is_active": False,
    },
]


def _types_fetches(fake):
    return [c for c in fake.calls if c[0] == "get_event_types"]


def test_events_search_resolves_values_display_names_and_mixed_ids(fake):
    fake.event_types = _TYPES
    fake.event_types_v2 = []
    fake.responses["activity/events"] = {"count": 0, "next": None, "results": []}
    result = _run(
        [
            "events",
            "search",
            "--event-type",
            "geofence_break, high speed,33333333-3333-3333-3333-333333333333",
        ]
    )
    assert result.exit_code == 0, result.output
    params = next(c for c in fake.calls if c[0] == "_get" and c[1] == "activity/events")[2]
    assert params["event_type"] == [
        "11111111-1111-1111-1111-111111111111",  # by value
        "22222222-2222-2222-2222-222222222222",  # by display name, case-insensitive
        "33333333-3333-3333-3333-333333333333",  # id passed through
    ]
    # one v1 and one v2 listing for the whole flag, in that order
    assert [c[1] for c in _types_fetches(fake)] == ["v1.0", "v2.0"]


def test_events_search_with_only_ids_does_not_fetch_event_types(fake):
    fake.responses["activity/events"] = {"count": 0, "next": None, "results": []}
    result = _run(["events", "search", "--event-type", "11111111-1111-1111-1111-111111111111"])
    assert result.exit_code == 0, result.output
    assert _types_fetches(fake) == []


def test_events_search_unknown_name_lists_the_servers_values(fake):
    fake.event_types = _TYPES
    fake.event_types_v2 = []
    result = _run(["events", "search", "--event-type", "geofence_brake"])
    assert result.exit_code == 2
    assert "unknown event type 'geofence_brake'" in result.output
    assert "values on this server: geofence_break, high_speed, old_type" in result.output
    assert not any(c[0] == "_get" and c[1] == "activity/events" for c in fake.calls)


def test_events_search_without_event_type_is_unchanged(fake):
    fake.responses["activity/events"] = {"count": 0, "next": None, "results": []}
    result = _run(["events", "search", "--state", "active"])
    assert result.exit_code == 0, result.output
    assert _types_fetches(fake) == []


_V2_ONLY = {
    "id": "44444444-4444-4444-4444-444444444444",
    "value": "animal_sighting",
    "display": "Animal Sighting",
}


def test_events_search_resolves_types_that_only_the_v2_listing_has(fake):
    # das's v1 listing filters to version 1, so a type created by this CLI
    # (always v2) is absent from it and must come from the v2 listing —
    # served here in paginated form to show the normalizer handles it
    fake.event_types = _TYPES
    fake.event_types_v2 = {"count": 1, "next": None, "results": [_V2_ONLY]}
    fake.responses["activity/events"] = {"count": 0, "next": None, "results": []}
    result = _run(["events", "search", "--event-type", "animal_sighting,Geofence Break"])
    assert result.exit_code == 0, result.output
    params = next(c for c in fake.calls if c[0] == "_get" and c[1] == "activity/events")[2]
    assert params["event_type"] == [_V2_ONLY["id"], _TYPES[0]["id"]]
    assert [c[1] for c in _types_fetches(fake)] == ["v1.0", "v2.0"]


def test_events_search_rejects_an_ambiguous_display_name(fake):
    fake.event_types = _TYPES + [
        {"id": "55555555-5555-5555-5555-555555555555", "value": "sighting", "display": "Sighting"},
        {
            "id": "66666666-6666-6666-6666-666666666666",
            "value": "sighting_v2",
            "display": "SIGHTING",
        },
    ]
    fake.event_types_v2 = []
    result = _run(["events", "search", "--event-type", "sighting"])  # exact value: fine
    assert result.exit_code == 0, result.output
    params = next(c for c in fake.calls if c[0] == "_get" and c[1] == "activity/events")[2]
    assert params["event_type"] == ["55555555-5555-5555-5555-555555555555"]

    result = _run(["events", "search", "--event-type", "Sighting"])  # display: two matches
    assert result.exit_code == 2
    assert "display name 'Sighting' matches 2 event types: " in result.output
    assert "sighting (55555555-5555-5555-5555-555555555555)" in result.output
    assert "sighting_v2 (66666666-6666-6666-6666-666666666666)" in result.output
    assert "Pass the value or id instead." in result.output


def test_events_search_exact_value_beats_a_colliding_display_name(fake):
    # a type whose *display* equals another type's *value* must not hijack it
    fake.event_types = [
        {"id": "77777777-7777-7777-7777-777777777777", "value": "fire", "display": "Fire"},
        {"id": "88888888-8888-8888-8888-888888888888", "value": "wildfire", "display": "fire"},
    ]
    fake.event_types_v2 = []
    fake.responses["activity/events"] = {"count": 0, "next": None, "results": []}
    result = _run(["events", "search", "--event-type", "fire"])
    assert result.exit_code == 0, result.output
    params = next(c for c in fake.calls if c[0] == "_get" and c[1] == "activity/events")[2]
    assert params["event_type"] == ["77777777-7777-7777-7777-777777777777"]


def test_fields_and_format_on_a_read_command(fake):
    fake.responses["subjects"] = {
        "count": 2,
        "next": None,
        "results": [
            {
                "id": "s1",
                "name": "Alpha",
                "last_position": {"geometry": {"coordinates": [36.8, -1.3]}},
            },
            {"id": "s2", "name": "Beta, Jr"},
        ],
    }
    result = _run(
        ["subjects", "search", "--fields", "id,name,last_position.geometry.coordinates.1"]
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["records"][0] == {
        "id": "s1",
        "name": "Alpha",
        "last_position.geometry.coordinates.1": -1.3,
    }
    assert doc["records"][1]["last_position.geometry.coordinates.1"] is None
    assert doc["meta"]["total"] == 2

    result = _run(["subjects", "search", "--fields", "id,name", "--format", "csv"])
    assert result.output == 'id,name\ns1,Alpha\ns2,"Beta, Jr"\n'

    result = _run(["subjects", "search", "--format", "tsv"])
    assert result.exit_code == 2
    assert "--format tsv needs --fields" in result.output


def test_plain_reads_never_fetch_the_clock(fake):
    # the extra GET /status is paid only by --today/--yesterday/--last and `er now`
    fake.responses["regions"] = [{"id": "r1"}]
    doc = json.loads(_run(["regions", "list"]).output)
    assert "server_utc" not in doc["meta"] and "site_tz" not in doc["meta"]
    assert [c[0] for c in fake.calls if c[0] in ("_get", "_get_response")] == ["_get"]


def test_now_prints_the_site_clock_as_one_record(fake):
    result = _run(["now"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["records"] == [
        {
            "utc": "2026-10-09T09:00:00Z",
            "site_now": "2026-10-09T12:00:00+03:00",
            "site_tz": "Africa/Nairobi",
            "today": {"since": "2026-10-09T00:00:00+03:00", "until": "2026-10-09T23:59:59+03:00"},
        }
    ]
    assert doc["meta"]["total"] == 1
    result = _run(["now", "--fields", "today.since", "--format", "tsv"])
    assert result.output == "today.since\n2026-10-09T00:00:00+03:00\n"


def test_events_search_today_folds_into_filter_and_reports_window(fake):
    fake.responses["activity/events"] = {"count": 0, "next": None, "results": []}
    result = _run(["events", "search", "--today", "--filter", '{"text":"lion"}'])
    assert result.exit_code == 0, result.output
    sent = json.loads(_gets(fake)[0][2]["filter"])
    assert sent == {
        "text": "lion",
        "date_range": {"lower": "2026-10-09T00:00:00+03:00", "upper": "2026-10-09T23:59:59+03:00"},
    }
    meta = json.loads(result.output)["meta"]
    assert meta["window"] == {
        "since": "2026-10-09T00:00:00+03:00",
        "until": "2026-10-09T23:59:59+03:00",
        "tz": "Africa/Nairobi",
        "mode": "today",
    }
    # the clock was fetched once and reused for meta
    assert [c for c in fake.calls if c[0] == "_get_response"] == [("_get_response", "status", None)]
    assert meta["server_utc"] == "2026-10-09T09:00:00Z"
    assert meta["site_now"] == "2026-10-09T12:00:00+03:00"
    assert meta["site_tz"] == "Africa/Nairobi"


def test_patrols_last_and_bare_until(fake):
    fake.responses["activity/patrols"] = {"count": 0, "next": None, "results": []}
    result = _run(["patrols", "search", "--last", "7d"])
    assert result.exit_code == 0, result.output
    window = json.loads(_gets(fake)[0][2]["filter"])["date_range"]
    assert window == {"lower": "2026-10-02T12:00:00+03:00", "upper": "2026-10-09T12:00:00+03:00"}
    result = _run(["patrols", "search", "--since", "2026-08-01", "--until", "2026-08-31"])
    window = json.loads(_gets(fake)[1][2]["filter"])["date_range"]
    assert window == {"lower": "2026-08-01", "upper": "2026-08-31T23:59:59.999999"}


def test_tracks_and_observations_take_the_window_as_params(fake):
    fake.responses["subject/s1/tracks"] = {"type": "FeatureCollection", "features": []}
    result = _run(["tracks", "get", "s1", "--yesterday"])
    assert result.exit_code == 0, result.output
    assert _gets(fake)[0][2] == {
        "since": "2026-10-08T00:00:00+03:00",
        "until": "2026-10-08T23:59:59+03:00",
    }
    fake.responses["observations"] = {"count": 0, "next": None, "results": []}
    result = _run(["observations", "search", "--subject-id", "s1", "--last", "24h"])
    assert result.exit_code == 0, result.output
    sent = _gets(fake)[1][2]
    assert sent["since"] == "2026-10-08T12:00:00+03:00"
    assert sent["until"] == "2026-10-09T12:00:00+03:00"
    assert "defaulting" not in result.stderr


def test_window_flags_conflict_is_a_usage_error_before_connecting(fake, monkeypatch):
    monkeypatch.setattr(cli_mod, "_connect", lambda ctx: pytest.fail("connected"))
    result = _run(["events", "search", "--today", "--since", "2026-01-01"])
    assert result.exit_code == 2
    assert "--today sets the whole window; drop --since" in result.output


def test_today_without_site_timezone_exits_1(fake):
    fake.status = {"server_timezone": "EAT"}
    result = _run(["events", "search", "--today"])
    assert result.exit_code == 1
    assert "no usable timezone (EAT)" in result.output
    assert _gets(fake) == []  # refused before asking for events
