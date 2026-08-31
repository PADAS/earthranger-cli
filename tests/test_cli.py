import pytest
from click.testing import CliRunner

import er_events_cli.cli as cli_mod
from conftest import FakeER
from er_events_cli.cli import main

SPEC_YAML = """
category: {value: wm, display: Wildlife Monitoring}
event_types:
  - value: sighting
    display: Sighting
    fields:
      - {key: notes, label: Notes, type: string}
"""

BAD_SPEC_YAML = """
category: {value: wm}
event_types: []
"""


@pytest.fixture
def fake(monkeypatch):
    fake = FakeER()
    monkeypatch.setattr(cli_mod, "_connect", lambda ctx: fake)
    return fake


def _run(args, spec_text=SPEC_YAML, input=None):
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("spec.yaml", "w") as f:
            f.write(spec_text)
        return runner.invoke(main, args, input=input, catch_exceptions=False)


def test_apply_fresh_site(fake):
    result = _run(["apply", "spec.yaml"])
    assert result.exit_code == 0
    assert "category    created      wm" in result.output
    assert "event_type  created      sighting" in result.output
    assert any(c[0] == "post_event_type" for c in fake.calls)


def test_apply_dry_run_prefixes_and_writes_nothing(fake):
    result = _run(["apply", "spec.yaml", "--dry-run"])
    assert result.exit_code == 0
    assert "would-created" in result.output
    assert fake.writes() == []


def test_apply_bad_spec_lists_all_errors_and_exits_1(fake):
    result = _run(["apply", "spec.yaml"], spec_text=BAD_SPEC_YAML)
    assert result.exit_code == 1
    assert "category.display: required string" in result.output
    assert "event_types: at least one event type is required" in result.output
    assert fake.calls == []


def test_apply_invalid_yaml_exits_cleanly(fake):
    result = _run(["apply", "spec.yaml"], spec_text="category: {value: [\n")
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "invalid YAML" in result.output


def test_connect_requires_server(monkeypatch):
    monkeypatch.delenv("ER_SERVER", raising=False)
    monkeypatch.delenv("ER_USERNAME", raising=False)
    monkeypatch.delenv("ER_PASSWORD", raising=False)
    result = _run(["apply", "spec.yaml"])
    assert result.exit_code != 0
    assert "Missing server" in result.output


def test_connect_prompts_for_password(monkeypatch):
    captured = {}

    def fake_make_client(*, server, username, password):
        captured.update(server=server, username=username, password=password)
        return FakeER()

    monkeypatch.setattr(cli_mod, "make_client", fake_make_client)
    result = _run(
        ["--server", "myreserve", "--username", "u", "apply", "spec.yaml"],
        input="secret\n",
    )
    assert result.exit_code == 0
    assert captured == {"server": "myreserve", "username": "u", "password": "secret"}


def test_api_errors_print_cleanly_and_exit_1(fake):
    from erclient.er_errors import ERClientBadCredentials

    def bad_creds(include_inactive=False):
        raise ERClientBadCredentials("Invalid credentials given.")

    fake.get_event_categories = bad_creds
    result = _run(["apply", "spec.yaml"])
    assert result.exit_code == 1
    assert "error: Invalid credentials given." in result.output


def test_network_errors_print_cleanly_and_exit_1(fake):
    import requests.exceptions

    def connection_error(include_inactive=False):
        raise requests.exceptions.ConnectionError("boom")

    fake.get_event_categories = connection_error
    result = _run(["apply", "spec.yaml"])
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "boom" in result.output


import json


def test_post_event_flags(fake):
    result = _run(
        [
            "post-event",
            "--event-type",
            "sighting",
            "--field",
            "species=elephant",
            "--field",
            "count=3",
            "--location",
            "-1.286,36.817",
            "--title",
            "Morning",
        ]
    )
    assert result.exit_code == 0
    assert "posted   sighting" in result.output
    posted = next(c[1] for c in fake.calls if c[0] == "post_event")
    assert posted["event_details"] == {"species": "elephant", "count": 3}
    assert posted["location"] == {"latitude": -1.286, "longitude": 36.817}
    assert posted["title"] == "Morning"


def test_post_event_file_invalid_yaml_exits_cleanly(fake):
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("events.yaml", "w") as f:
            f.write("category: {value: [\n")
        result = runner.invoke(main, ["post-event", "--file", "events.yaml"])
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "invalid YAML" in result.output


def test_post_event_requires_type_or_file(fake):
    result = _run(["post-event"])
    assert result.exit_code != 0


def test_post_event_batch_partial_failure_exits_1(fake):
    def failing_post(event):
        if event["event_details"].get("species") == "lion":
            raise RuntimeError("boom")
        return event

    fake.post_event = failing_post
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("events.yaml", "w") as f:
            f.write(
                "- event_type: s\n  event_details: {species: elephant}\n"
                "- event_type: s\n  event_details: {species: lion}\n"
            )
        result = runner.invoke(main, ["post-event", "--file", "events.yaml"])
    assert result.exit_code == 1
    assert "posted   s" in result.output
    assert "FAILED   s: boom" in result.output


def test_list_categories(fake):
    fake.categories = [
        {"value": "wm", "display": "Wildlife Monitoring", "is_active": True},
        {"value": "old", "display": "Old Category", "is_active": False},
    ]
    result = _run(["list", "categories"])
    assert result.exit_code == 0
    assert "wm" in result.output
    assert "(inactive)" in result.output


def test_list_event_types_filters_by_category(fake):
    fake.event_types = [
        {"value": "a", "display": "A", "category": {"value": "wm"}, "is_active": True},
        {"value": "b", "display": "B", "category": "other", "is_active": True},
    ]
    result = _run(["list", "event-types", "--category", "wm"])
    assert result.exit_code == 0
    assert "a" in result.output
    assert " b " not in result.output


def test_show_event_type_includes_choices(fake):
    schema = {
        "json": {
            "properties": {
                "species": {"anyOf": [{"$ref": "/api/v2.0/schemas/choices.json?field=s_species"}]}
            }
        }
    }
    fake.event_types = [
        {"value": "s", "display": "S", "category": "wm", "schema": schema},
    ]
    fake.choices = {"s_species": [{"id": "1", "value": "elephant", "display": "Elephant"}]}
    result = _run(["show", "event-type", "s"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["event_type"]["value"] == "s"
    assert data["choices"]["s_species"][0]["value"] == "elephant"


def test_show_event_type_missing_exits_1(fake):
    result = _run(["show", "event-type", "nope"])
    assert result.exit_code == 1
    assert "no event type with value 'nope'" in result.output
