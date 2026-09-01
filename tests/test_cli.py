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
    result = _run(["events", "apply", "spec.yaml"])
    assert result.exit_code == 0
    assert "category    created      wm" in result.output
    assert "event_type  created      sighting" in result.output
    assert any(c[0] == "post_event_type" for c in fake.calls)


def test_apply_dry_run_prefixes_and_writes_nothing(fake):
    result = _run(["events", "apply", "spec.yaml", "--dry-run"])
    assert result.exit_code == 0
    assert "would-created" in result.output
    assert fake.writes() == []


def test_apply_bad_spec_lists_all_errors_and_exits_1(fake):
    result = _run(["events", "apply", "spec.yaml"], spec_text=BAD_SPEC_YAML)
    assert result.exit_code == 1
    assert "category.display: required string" in result.output
    assert "event_types: at least one event type is required" in result.output
    assert fake.calls == []


def test_apply_invalid_yaml_exits_cleanly(fake):
    result = _run(["events", "apply", "spec.yaml"], spec_text="category: {value: [\n")
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "invalid YAML" in result.output


def test_connect_requires_server(monkeypatch):
    monkeypatch.delenv("ER_SERVER", raising=False)
    monkeypatch.delenv("ER_USERNAME", raising=False)
    monkeypatch.delenv("ER_PASSWORD", raising=False)
    result = _run(["events", "apply", "spec.yaml"])
    assert result.exit_code != 0
    assert "Missing server" in result.output


def test_connect_prompts_for_password(monkeypatch):
    captured = {}

    def fake_make_client(*, server, username, password):
        captured.update(server=server, username=username, password=password)
        return FakeER()

    monkeypatch.setattr(cli_mod, "make_client", fake_make_client)
    result = _run(
        ["--server", "myreserve", "--username", "u", "events", "apply", "spec.yaml"],
        input="secret\n",
    )
    assert result.exit_code == 0
    assert captured == {"server": "myreserve", "username": "u", "password": "secret"}


def test_api_errors_print_cleanly_and_exit_1(fake):
    from erclient.er_errors import ERClientBadCredentials

    def bad_creds(include_inactive=False):
        raise ERClientBadCredentials("Invalid credentials given.")

    fake.get_event_categories = bad_creds
    result = _run(["events", "apply", "spec.yaml"])
    assert result.exit_code == 1
    assert "error: Invalid credentials given." in result.output


def test_network_errors_print_cleanly_and_exit_1(fake):
    import requests.exceptions

    def connection_error(include_inactive=False):
        raise requests.exceptions.ConnectionError("boom")

    fake.get_event_categories = connection_error
    result = _run(["events", "apply", "spec.yaml"])
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "boom" in result.output


import json


def test_post_event_flags(fake):
    result = _run(
        [
            "events",
            "post",
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
        result = runner.invoke(main, ["events", "post", "--file", "events.yaml"])
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "invalid YAML" in result.output


def test_post_event_requires_type_or_file(fake):
    result = _run(["events", "post"])
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
        result = runner.invoke(main, ["events", "post", "--file", "events.yaml"])
    assert result.exit_code == 1
    assert "posted   s" in result.output
    assert "FAILED   s: boom" in result.output


def test_list_categories(fake):
    fake.categories = [
        {"value": "wm", "display": "Wildlife Monitoring", "is_active": True},
        {"value": "old", "display": "Old Category", "is_active": False},
    ]
    result = _run(["events", "list", "categories"])
    assert result.exit_code == 0
    assert "wm" in result.output
    assert "(inactive)" in result.output


def test_list_event_types_filters_by_category(fake):
    fake.event_types = [
        {"value": "a", "display": "A", "category": {"value": "wm"}, "is_active": True},
        {"value": "b", "display": "B", "category": "other", "is_active": True},
    ]
    result = _run(["events", "list", "event-types", "--category", "wm"])
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
    result = _run(["events", "show", "event-type", "s"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["event_type"]["value"] == "s"
    assert data["choices"]["s_species"][0]["value"] == "elephant"


def test_show_event_type_handles_stringified_schema(fake):
    # ER sometimes returns the schema JSON-stringified on GET.
    schema = {
        "json": {
            "properties": {
                "species": {"anyOf": [{"$ref": "/api/v2.0/schemas/choices.json?field=s_species"}]}
            }
        }
    }
    fake.event_types = [
        {"value": "s", "display": "S", "category": "wm", "schema": json.dumps(schema)},
    ]
    fake.choices = {"s_species": [{"id": "1", "value": "elephant", "display": "Elephant"}]}
    result = _run(["events", "show", "event-type", "s"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["choices"]["s_species"][0]["value"] == "elephant"


def test_show_event_type_missing_exits_1(fake):
    result = _run(["events", "show", "event-type", "nope"])
    assert result.exit_code == 1
    assert "no event type with value 'nope'" in result.output


# --- auth subcommands & cached-token connection ---

from datetime import UTC, datetime, timedelta

from er_events_cli import token_store

FUTURE = datetime.now(UTC) + timedelta(days=30)
PAST = datetime.now(UTC) - timedelta(days=1)
AUTH = {"access_token": "acc-1", "refresh_token": "ref-1", "token_type": "Bearer"}


class FakeLoginClient(FakeER):
    def __init__(self, succeed=True):
        super().__init__()
        self._succeed = succeed

    def login(self):
        if self._succeed:
            self.auth = dict(AUTH)
            self.auth_expires = FUTURE
        return self._succeed


def test_auth_login_caches_token(monkeypatch):
    monkeypatch.setattr(cli_mod, "make_client", lambda **kw: FakeLoginClient())
    result = _run(["--server", "sandbox", "--username", "u", "auth", "login"], input="pw\n")
    assert result.exit_code == 0
    assert "Authenticated. Token cached for sandbox.pamdas.org." in result.output
    data = token_store.load_token("sandbox.pamdas.org")
    assert data["access_token"] == "acc-1"
    assert data["username"] == "u"


def test_auth_login_failure_exits_1(monkeypatch):
    monkeypatch.setattr(cli_mod, "make_client", lambda **kw: FakeLoginClient(succeed=False))
    result = _run(["--server", "sandbox", "--username", "u", "--password", "bad", "auth", "login"])
    assert result.exit_code == 1
    assert "error: login failed for 'u' at sandbox.pamdas.org" in result.output
    assert token_store.load_token("sandbox.pamdas.org") is None


def test_auth_status_and_logout():
    result = _run(["--server", "sandbox", "auth", "status"])
    assert "sandbox.pamdas.org: not authenticated" in result.output
    token_store.save_token("sandbox.pamdas.org", AUTH, FUTURE, "chris")
    result = _run(["--server", "sandbox", "auth", "status"])
    assert "sandbox.pamdas.org: valid as chris" in result.output
    token_store.save_token("sandbox.pamdas.org", AUTH, PAST, "chris")
    result = _run(["--server", "sandbox", "auth", "status"])
    assert "sandbox.pamdas.org: expired" in result.output
    result = _run(["--server", "sandbox", "auth", "logout"])
    assert "Logged out." in result.output
    result = _run(["--server", "sandbox", "auth", "logout"])
    assert "No cached token." in result.output


def test_connect_uses_cached_token_without_password(monkeypatch):
    token_store.save_token("sandbox.pamdas.org", AUTH, FUTURE, "chris")
    fake = FakeER()
    monkeypatch.setattr(cli_mod, "make_token_client", lambda **kw: fake)
    result = _run(["--server", "sandbox", "events", "list", "categories"])
    assert result.exit_code == 0
    assert fake.auth["access_token"] == "acc-1"
    assert ("auth_headers",) in fake.calls


def test_connect_explicit_password_beats_cache(monkeypatch):
    token_store.save_token("sandbox.pamdas.org", AUTH, FUTURE, "chris")
    captured = {}

    def fake_make_client(*, server, username, password):
        captured.update(username=username, password=password)
        return FakeER()

    monkeypatch.setattr(cli_mod, "make_client", fake_make_client)
    result = _run(
        [
            "--server",
            "sandbox",
            "--username",
            "u",
            "--password",
            "pw",
            "events",
            "list",
            "categories",
        ]
    )
    assert result.exit_code == 0
    assert captured == {"username": "u", "password": "pw"}


def test_connect_cached_token_skipped_when_username_differs(monkeypatch):
    # a cached token belongs to "chris"; an explicit --username alice must not
    # silently ride on chris's cached session.
    token_store.save_token("sandbox.pamdas.org", AUTH, FUTURE, "chris")
    captured = {}

    def fake_make_client(*, server, username, password):
        captured.update(server=server, username=username, password=password)
        return FakeER()

    monkeypatch.setattr(cli_mod, "make_client", fake_make_client)
    result = _run(
        ["--server", "sandbox", "--username", "alice", "events", "list", "categories"], input="pw\n"
    )
    assert result.exit_code == 0
    assert captured == {"server": "sandbox", "username": "alice", "password": "pw"}


def test_connect_cached_token_used_when_username_matches(monkeypatch):
    token_store.save_token("sandbox.pamdas.org", AUTH, FUTURE, "chris")
    fake = FakeER()
    monkeypatch.setattr(cli_mod, "make_token_client", lambda **kw: fake)
    result = _run(["--server", "sandbox", "--username", "chris", "events", "list", "categories"])
    assert result.exit_code == 0
    assert fake.auth["access_token"] == "acc-1"
    assert ("auth_headers",) in fake.calls


def test_connect_expired_cached_session_message(monkeypatch):
    from erclient.er_errors import ERClientException

    token_store.save_token("sandbox.pamdas.org", AUTH, PAST, "chris")
    fake = FakeER()

    def failing_auth_headers():
        raise ERClientException("Login failed.")

    fake.auth_headers = failing_auth_headers
    monkeypatch.setattr(cli_mod, "make_token_client", lambda **kw: fake)
    result = _run(["--server", "sandbox", "events", "list", "categories"])
    assert result.exit_code == 1
    assert (
        "error: cached session for sandbox.pamdas.org expired or invalid — "
        "run 'er auth login'" in result.output
    )


def test_rotated_token_is_persisted_after_command(monkeypatch):
    token_store.save_token("sandbox.pamdas.org", AUTH, PAST, "chris")
    fake = FakeER()

    def rotating_auth_headers():
        fake.auth = {"access_token": "acc-2", "refresh_token": "ref-2", "token_type": "Bearer"}
        fake.auth_expires = FUTURE
        return {}

    fake.auth_headers = rotating_auth_headers
    monkeypatch.setattr(cli_mod, "make_token_client", lambda **kw: fake)
    result = _run(["--server", "sandbox", "events", "list", "categories"])
    assert result.exit_code == 0
    data = token_store.load_token("sandbox.pamdas.org")
    assert data["access_token"] == "acc-2"
    assert data["refresh_token"] == "ref-2"
    assert data["username"] == "chris"


# --- pull command ---


def _seed_pull_server(fake):
    from er_events_cli.dsl import parse_spec as _ps
    from er_events_cli.schema_gen import build_event_type_payload as _bp

    spec = _ps(
        {
            "category": {"value": "wm", "display": "Wildlife Monitoring"},
            "event_types": [
                {
                    "value": "sighting",
                    "display": "Sighting",
                    "fields": [
                        {
                            "key": "species",
                            "label": "Species",
                            "type": "select",
                            "options": ["elephant"],
                        },
                    ],
                }
            ],
        }
    )
    payload = _bp(spec.event_types[0], "wm")
    payload["id"] = "et-1"
    payload["category"] = {"value": "wm"}
    fake.categories = [{"id": "c1", "value": "wm", "display": "Wildlife Monitoring"}]
    fake.event_types = [payload]
    fake.choices = {
        "sighting_species": [
            {"id": "1", "value": "elephant", "display": "Elephant", "is_active": True}
        ]
    }
    return payload


def test_pull_prints_spec_yaml(fake):
    _seed_pull_server(fake)
    result = _run(["events", "pull", "wm"])
    assert result.exit_code == 0
    assert "value: wm" in result.output
    assert "- elephant" in result.output


def test_pull_writes_file(fake):
    _seed_pull_server(fake)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["events", "pull", "wm", "-o", "out.yaml"])
        assert result.exit_code == 0
        with open("out.yaml") as f:
            text = f.read()
    assert "Wrote out.yaml" in result.output
    assert "value: wm" in text


def test_pull_refuses_lossy_without_flag(fake):
    payload = _seed_pull_server(fake)
    payload["schema"]["ui"]["fields"]["species"]["type"] = "LOCATION"
    result = _run(["events", "pull", "wm"])
    assert result.exit_code == 1
    assert "warning:" in result.output
    assert "--skip-unsupported" in result.output
    result = _run(["events", "pull", "wm", "--skip-unsupported"])
    assert result.exit_code == 0
    assert "# Skipped constructs" in result.output


def test_pull_missing_category_exits_1(fake):
    fake.categories = []
    result = _run(["events", "pull", "nope"])
    assert result.exit_code == 1
    assert "error: no category with value 'nope'" in result.output


# --- profiles ---

from er_events_cli import config_store


def test_profile_add_use_list_remove():
    result = _run(["profile", "add", "sandbox", "--server", "sandbox", "--username", "chris"])
    assert result.exit_code == 0
    assert "Added profile 'sandbox' (sandbox.pamdas.org)." in result.output
    result = _run(["profile", "add", "prod", "--server", "myreserve"])
    assert "Added profile 'prod' (myreserve.pamdas.org)." in result.output
    result = _run(["--profile", "sandbox", "profile", "list"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert any(ln.startswith("* sandbox") and "chris" in ln for ln in lines)
    assert any(ln.startswith("  prod") for ln in lines)
    result = _run(["profile", "remove", "prod"])
    assert "Removed profile 'prod'." in result.output
    result = _run(["profile", "remove", "prod"])
    assert result.exit_code == 1
    assert "error: no profile named 'prod'" in result.output


def test_env_profile_supplies_server_and_username(monkeypatch):
    monkeypatch.setenv("ER_PROFILE", "sandbox")
    config_store.add_profile("sandbox", server="sandbox", username="chris")
    captured = {}

    def fake_make_client(*, server, username, password):
        captured.update(server=server, username=username, password=password)
        return FakeLoginClient()

    monkeypatch.setattr(cli_mod, "make_client", fake_make_client)
    monkeypatch.delenv("ER_SERVER", raising=False)
    monkeypatch.delenv("ER_USERNAME", raising=False)
    result = _run(["auth", "login"], input="pw\n")  # only the password is prompted
    assert result.exit_code == 0
    assert captured == {"server": "sandbox", "username": "chris", "password": "pw"}


def test_profile_flag_selects_profile(monkeypatch):
    config_store.add_profile("sandbox", server="sandbox")
    config_store.add_profile("prod", server="myreserve", username="ops")
    token_store.save_token("myreserve.pamdas.org", AUTH, FUTURE, "ops")
    fake = FakeER()
    seen = {}

    def fake_token_client(*, server):
        seen["server"] = server
        return fake

    monkeypatch.setattr(cli_mod, "make_token_client", fake_token_client)
    monkeypatch.delenv("ER_SERVER", raising=False)
    result = _run(["--profile", "prod", "events", "list", "categories"])
    assert result.exit_code == 0
    assert seen["server"] == "myreserve"


def test_explicit_server_flag_overrides_profile(monkeypatch):
    config_store.add_profile("sandbox", server="sandbox")
    token_store.save_token("other.pamdas.org", AUTH, FUTURE, "x")
    fake = FakeER()
    seen = {}

    def fake_token_client(*, server):
        seen["server"] = server
        return fake

    monkeypatch.setattr(cli_mod, "make_token_client", fake_token_client)
    result = _run(["--server", "other", "events", "list", "categories"])
    assert result.exit_code == 0
    assert seen["server"] == "other"


def test_unknown_profile_flag_is_usage_error():
    result = _run(["--profile", "zzz", "events", "list", "categories"])
    assert result.exit_code != 0
    assert "Unknown profile 'zzz'" in result.output


def test_profile_current():
    config_store.add_profile("sandbox", server="sandbox")
    result = _run(["profile", "current"])
    assert result.exit_code == 1
    assert result.output == ""
    result = _run(["--profile", "sandbox", "profile", "current"])
    assert result.exit_code == 0
    assert result.output == "sandbox\n"


def test_profile_use_prints_export_line():
    config_store.add_profile("prod", server="myreserve")
    result = _run(["profile", "use", "prod"])
    assert result.exit_code == 0
    assert result.output == "export ER_PROFILE=prod\n"


def test_profile_use_no_arg_prints_unset():
    result = _run(["profile", "use"])
    assert result.exit_code == 0
    assert result.output == "unset ER_PROFILE\n"


def test_profile_use_unknown_errors():
    result = _run(["profile", "use", "zzz"])
    assert result.exit_code == 1
    assert "error: no profile named 'zzz'" in result.output


def test_profile_show_by_name():
    config_store.add_profile("prod", server="myreserve", username="chris")
    token_store.save_token("myreserve.pamdas.org", AUTH, FUTURE, "chris")
    result = _run(["profile", "show", "prod"])
    assert result.exit_code == 0
    assert "name:      prod" in result.output
    assert "server:    myreserve" in result.output
    assert "host:      myreserve.pamdas.org" in result.output
    assert "username:  chris" in result.output
    assert "auth:      valid" in result.output


def test_profile_show_defaults_to_selection(monkeypatch):
    config_store.add_profile("sandbox", server="sandbox")
    monkeypatch.setenv("ER_PROFILE", "sandbox")
    result = _run(["profile", "show"])
    assert result.exit_code == 0
    assert "name:      sandbox" in result.output
    assert "username:  -" in result.output
    assert "auth:      not authenticated" in result.output


def test_profile_show_errors():
    result = _run(["profile", "show", "zzz"])
    assert result.exit_code == 1
    assert "error: no profile named 'zzz'" in result.output
    result = _run(["profile", "show"])
    assert result.exit_code != 0
    assert "no profile selected" in result.output


def test_connection_flags_accepted_after_subcommand(monkeypatch):
    captured = {}

    def fake_make_client(*, server, username, password):
        captured.update(server=server, username=username, password=password)
        return FakeLoginClient()

    monkeypatch.setattr(cli_mod, "make_client", fake_make_client)
    result = _run(
        ["auth", "login", "--server", "sandbox", "--username", "chrisd", "--password", "pw"]
    )
    assert result.exit_code == 0
    assert captured == {"server": "sandbox", "username": "chrisd", "password": "pw"}


def test_trailing_flags_override_globals(monkeypatch):
    config_store.add_profile("sandbox", server="sandbox")
    config_store.add_profile("prod", server="myreserve")
    token_store.save_token("myreserve.pamdas.org", AUTH, FUTURE, "ops")
    fake = FakeER()
    seen = {}

    def fake_token_client(*, server):
        seen["server"] = server
        return fake

    monkeypatch.setattr(cli_mod, "make_token_client", fake_token_client)
    result = _run(["--profile", "sandbox", "events", "list", "categories", "--profile", "prod"])
    assert result.exit_code == 0
    assert seen["server"] == "myreserve"
