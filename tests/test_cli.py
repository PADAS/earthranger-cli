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
