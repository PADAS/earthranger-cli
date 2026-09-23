# tests/test_read_commands.py
import json

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
        assert cmd.help and spec.help in cmd.help


def test_help_shows_the_endpoint():
    result = _run(["tracks", "get", "--help"])
    assert "[GET /api/v2.0/subject/{subject_id}/tracks]" in result.output
    result = _run(["subjects", "search", "--help"])
    assert "[GET /api/v1.0/subjects]" in result.output
    assert "--updated-since" in result.output and "--updated_since" in result.output


def test_list_paginates_and_emits_records_meta(fake):
    fake.responses["subjects"] = [
        {"count": 3, "next": "https://fake/api/v1.0/subjects/?page=2", "results": [{"id": "a"}, {"id": "b"}]}
    ]
    fake.responses["https://fake/api/v1.0/subjects/?page=2"] = {
        "count": 3, "next": None, "results": [{"id": "c"}]
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
        "count": 5, "next": "https://fake/?page=2", "results": [{"id": "a"}, {"id": "b"}]
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
    result = _run(["tracks", "get", "s-1", "--since", "2026-06-01T00:00:00Z", "--max-speed-kmh", "80"])
    assert result.exit_code == 0, result.output
    call = _gets(fake)[0]
    assert call[3] == "https://fake.pamdas.org/api/v2.0"
    assert call[2] == {"since": "2026-06-01T00:00:00Z", "max_speed_kmh": 80.0}
    assert json.loads(result.output)["records"][0]["type"] == "FeatureCollection"


def test_events_search_lives_under_existing_events_group(fake):
    fake.responses["activity/events"] = {"count": 1, "next": None, "results": [{"id": "e1"}]}
    result = _run(["events", "search", "--event-type", "uuid-1", "--state", "active"])
    assert result.exit_code == 0, result.output
    assert _gets(fake)[0][2] == {"event_type": "uuid-1", "state": "active", "page_size": 100}
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
    assert json.loads(target.read_text())["meta"] == {"total": 2, "pages": 1, "count_reported": 2}


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
