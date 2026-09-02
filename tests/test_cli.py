import pytest
from click.testing import CliRunner

import earthranger_cli.cli as cli_mod
from conftest import FakeER
from earthranger_cli.cli import main

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
    assert "error: credentials rejected (Invalid credentials given.)." in result.output


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

from earthranger_cli import token_store

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


def test_auth_login_requires_profile(monkeypatch):
    monkeypatch.setattr(cli_mod, "make_client", lambda **kw: FakeLoginClient())
    result = _run(["--server", "sandbox", "--username", "u", "auth", "login"], input="pw\n")
    assert result.exit_code != 0
    assert "selected profile is required" in result.output + result.stderr


def test_auth_login_caches_on_profile(monkeypatch):
    config_store.add_profile("dev", server="sandbox", username="u")
    monkeypatch.setenv("ER_PROFILE", "dev")
    monkeypatch.setattr(cli_mod, "make_client", lambda **kw: FakeLoginClient())
    result = _run(["auth", "login"], input="pw\n")
    assert result.exit_code == 0
    assert "Authenticated. Session cached on profile 'dev' (sandbox.pamdas.org)." in result.output
    data = token_store.load_token("dev")
    assert data["access_token"] == "acc-1"
    assert data["username"] == "u"


def test_auth_login_updates_profile_username(monkeypatch):
    config_store.add_profile("dev", server="sandbox", username="old")
    monkeypatch.setenv("ER_PROFILE", "dev")
    monkeypatch.setattr(cli_mod, "make_client", lambda **kw: FakeLoginClient())
    result = _run(["auth", "login", "--username", "alice", "--password", "pw"])
    assert result.exit_code == 0
    assert "Profile 'dev' username set to 'alice'." in result.output
    assert config_store.get_profile("dev")["username"] == "alice"
    assert token_store.load_token("dev")["username"] == "alice"


def test_auth_login_failure_exits_1(monkeypatch):
    config_store.add_profile("dev", server="sandbox")
    monkeypatch.setenv("ER_PROFILE", "dev")
    monkeypatch.setattr(cli_mod, "make_client", lambda **kw: FakeLoginClient(succeed=False))
    result = _run(["auth", "login", "--username", "u", "--password", "bad"])
    assert result.exit_code == 1
    assert "error: login failed for 'u' at sandbox.pamdas.org" in result.output
    assert token_store.load_token("dev") is None


def test_auth_status_and_logout(monkeypatch):
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    result = _run(["auth", "status"])
    assert "dev (sandbox.pamdas.org): not authenticated" in result.output
    token_store.save_token("dev", AUTH, FUTURE, "chris")
    result = _run(["auth", "status"])
    assert "dev (sandbox.pamdas.org): valid as chris" in result.output
    token_store.save_token("dev", AUTH, PAST, "chris")
    result = _run(["auth", "status"])
    assert "dev (sandbox.pamdas.org): expired" in result.output
    result = _run(["auth", "logout"])
    assert "Logged out." in result.output
    result = _run(["auth", "logout"])
    assert "No cached session." in result.output


def test_connect_uses_cached_token_without_password(monkeypatch):
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    token_store.save_token("dev", AUTH, FUTURE, "chris")
    fake = FakeER()
    monkeypatch.setattr(cli_mod, "make_token_client", lambda **kw: fake)
    result = _run(["events", "list", "categories"])
    assert result.exit_code == 0
    assert fake.auth["access_token"] == "acc-1"
    assert ("auth_headers",) in fake.calls


def test_connect_without_profile_never_uses_cache(monkeypatch):
    token_store.save_token("dev", AUTH, FUTURE, "chris")
    captured = {}

    def fake_make_client(*, server, username, password):
        captured.update(username=username, password=password)
        return FakeER()

    monkeypatch.setattr(cli_mod, "make_client", fake_make_client)
    result = _run(
        ["--server", "sandbox", "--username", "chris", "events", "list", "categories"],
        input="pw\n",
    )
    assert result.exit_code == 0
    assert captured["password"] == "pw"  # password path, not the cache


def test_connect_explicit_password_beats_cache(monkeypatch):
    # a valid session on the selected profile must still lose to an explicit
    # password (the token would otherwise be used — same server, no username
    # mismatch)
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    token_store.save_token("dev", AUTH, FUTURE, "chris")
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
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    token_store.save_token("dev", AUTH, FUTURE, "chris")
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
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    token_store.save_token("dev", AUTH, FUTURE, "chris")
    fake = FakeER()
    monkeypatch.setattr(cli_mod, "make_token_client", lambda **kw: fake)
    result = _run(["--server", "sandbox", "--username", "chris", "events", "list", "categories"])
    assert result.exit_code == 0
    assert fake.auth["access_token"] == "acc-1"
    assert ("auth_headers",) in fake.calls


def test_connect_expired_cached_session_message(monkeypatch):
    from erclient.er_errors import ERClientException

    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    token_store.save_token("dev", AUTH, PAST, "chris")
    fake = FakeER()

    def failing_auth_headers():
        raise ERClientException("Login failed.")

    fake.auth_headers = failing_auth_headers
    monkeypatch.setattr(cli_mod, "make_token_client", lambda **kw: fake)
    result = _run(["--server", "sandbox", "events", "list", "categories"])
    assert result.exit_code == 1
    assert (
        "error: cached session for profile 'dev' expired or invalid — "
        "run 'er auth login'" in result.output
    )


def test_rotated_token_is_persisted_after_command(monkeypatch):
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    token_store.save_token("dev", AUTH, PAST, "chris")
    fake = FakeER()

    def rotating_auth_headers():
        fake.auth = {"access_token": "acc-2", "refresh_token": "ref-2", "token_type": "Bearer"}
        fake.auth_expires = FUTURE
        return {}

    fake.auth_headers = rotating_auth_headers
    monkeypatch.setattr(cli_mod, "make_token_client", lambda **kw: fake)
    result = _run(["--server", "sandbox", "events", "list", "categories"])
    assert result.exit_code == 0
    data = token_store.load_token("dev")
    assert data["access_token"] == "acc-2"
    assert data["refresh_token"] == "ref-2"
    assert data["username"] == "chris"


# --- pull command ---


def _seed_pull_server(fake):
    from earthranger_cli.dsl import parse_spec as _ps
    from earthranger_cli.schema_gen import build_event_type_payload as _bp

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

from earthranger_cli import config_store


def test_profile_add_use_list_remove():
    result = _run(["profile", "add", "sandbox", "--server", "sandbox", "--username", "chris"])
    assert result.exit_code == 0
    assert "Added profile 'sandbox' (sandbox.pamdas.org)." in result.stderr
    assert result.stdout == "export ER_PROFILE=sandbox\n"  # eval-able: auto-switch
    result = _run(["profile", "add", "prod", "--server", "myreserve"])
    assert "Added profile 'prod' (myreserve.pamdas.org)." in result.stderr
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
    token_store.save_token("prod", AUTH, FUTURE, "ops")
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


def test_explicit_server_flag_overrides_profile_server(monkeypatch):
    # --server rewires the host; the profile's session stays home (see
    # test_server_override_bypasses_cached_session), so the override
    # authenticates with a password against the override host
    config_store.add_profile("dev", server="sandbox", username="x")
    monkeypatch.setenv("ER_PROFILE", "dev")
    token_store.save_token("dev", AUTH, FUTURE, "x")
    seen = {}

    def fake_make_client(*, server, username, password):
        seen.update(server=server, username=username, password=password)
        return FakeER()

    monkeypatch.setattr(cli_mod, "make_client", fake_make_client)
    result = _run(["--server", "other", "--password", "pw", "events", "list", "categories"])
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
    token_store.save_token("prod", AUTH, FUTURE, "chris")
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
    config_store.add_profile("dev", server="sandbox")
    result = _run(
        [
            "auth",
            "login",
            "--server",
            "sandbox",
            "--username",
            "chrisd",
            "--password",
            "pw",
            "--profile",
            "dev",
        ]
    )
    assert result.exit_code == 0
    assert captured == {"server": "sandbox", "username": "chrisd", "password": "pw"}


def test_trailing_flags_override_globals(monkeypatch):
    config_store.add_profile("sandbox", server="sandbox")
    config_store.add_profile("prod", server="myreserve")
    token_store.save_token("prod", AUTH, FUTURE, "ops")
    fake = FakeER()
    seen = {}

    def fake_token_client(*, server):
        seen["server"] = server
        return fake

    monkeypatch.setattr(cli_mod, "make_token_client", fake_token_client)
    result = _run(["--profile", "sandbox", "events", "list", "categories", "--profile", "prod"])
    assert result.exit_code == 0
    assert seen["server"] == "myreserve"


def _seed_choice_sets(fake):
    schema = {
        "json": {
            "properties": {
                "species": {"anyOf": [{"$ref": "/api/v2.0/schemas/choices.json?field=s_species"}]}
            }
        }
    }
    fake.event_types = [{"value": "s", "display": "S", "category": "wm", "schema": schema}]
    fake.choices = {
        "s_species": [
            {
                "id": "1",
                "field": "s_species",
                "value": "b_lion",
                "display": "Lion",
                "is_active": True,
                "ordernum": 1,
            },
            {
                "id": "2",
                "field": "s_species",
                "value": "a_ele",
                "display": "Elephant",
                "is_active": True,
                "ordernum": 0,
                "icon": "ele_icon",
            },
            {
                "id": "3",
                "field": "s_species",
                "value": "old",
                "display": "Old",
                "is_active": False,
                "ordernum": 2,
            },
        ],
        "orphan_set": [
            {"id": "4", "field": "orphan_set", "value": "x", "display": "X", "is_active": True},
        ],
    }


def test_choices_list(fake):
    _seed_choice_sets(fake)
    result = _run(["choices", "list"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert any("s_species" in ln and "2 active" in ln and "1 inactive" in ln for ln in lines)
    assert any("orphan_set" in ln and "(unreferenced)" in ln for ln in lines)
    assert not any("s_species" in ln and "(unreferenced)" in ln for ln in lines)


def test_choices_show(fake):
    _seed_choice_sets(fake)
    result = _run(["choices", "show", "s_species"])
    assert result.exit_code == 0
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert lines[0].startswith("a_ele")
    assert "icon=ele_icon" in lines[0]
    assert lines[1].startswith("b_lion")
    assert "(inactive)" in lines[2]


def test_choices_show_missing_exits_1(fake):
    result = _run(["choices", "show", "nope"])
    assert result.exit_code == 1
    assert "no choices found for field 'nope'" in result.output


def test_profile_set_updates_selected_profile(monkeypatch):
    config_store.add_profile("sandbox", server="sandbox")
    monkeypatch.setenv("ER_PROFILE", "sandbox")
    result = _run(["profile", "set", "username", "me"])
    assert result.exit_code == 0
    assert "Set username for profile 'sandbox'." in result.output
    assert config_store.get_profile("sandbox") == {"server": "sandbox", "username": "me"}


def test_profile_set_with_trailing_profile_flag():
    config_store.add_profile("sandbox", server="sandbox")
    config_store.add_profile("prod", server="old-host")
    result = _run(["profile", "set", "server", "https://new.example.org", "--profile", "prod"])
    assert result.exit_code == 0
    assert config_store.get_profile("prod")["server"] == "https://new.example.org"
    assert config_store.get_profile("sandbox")["server"] == "sandbox"


def test_profile_set_errors(monkeypatch):
    result = _run(["profile", "set", "username", "me"])  # nothing selected
    assert result.exit_code != 0
    assert "no profile selected" in result.output + result.stderr

    config_store.add_profile("sandbox", server="sandbox")
    monkeypatch.setenv("ER_PROFILE", "sandbox")
    result = _run(["profile", "set", "color", "green"])
    assert result.exit_code == 1
    assert "server, username" in result.output

    monkeypatch.setenv("ER_PROFILE", "zzz")
    result = _run(["profile", "set", "username", "me"])
    assert result.exit_code == 1
    assert "no profile named 'zzz'" in result.output


def test_server_override_bypasses_cached_session(monkeypatch):
    # F4/r-suppressed cli:110 — never send a profile's token to another host
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    token_store.save_token("dev", AUTH, FUTURE, "chris")
    captured = {}

    def fake_make_client(*, server, username, password):
        captured.update(server=server, password=password)
        return FakeER()

    monkeypatch.setattr(cli_mod, "make_client", fake_make_client)
    result = _run(["--server", "other", "events", "list", "categories"], input="pw\n")
    assert result.exit_code == 0
    assert captured == {"server": "other", "password": "pw"}  # password path


def test_auth_login_rejects_server_mismatch(monkeypatch):
    # F5/r-suppressed cli:315
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    result = _run(["auth", "login", "--server", "other", "--password", "pw"])
    assert result.exit_code != 0
    out = result.output + result.stderr
    assert "differs from profile" in out and "er profile set server" in out
    assert token_store.load_token("dev") is None


def test_profile_add_overwrite_invalidates_session():
    # F2/r3910689341
    config_store.add_profile("dev", server="sandbox", username="chris")
    token_store.save_token("dev", AUTH, FUTURE, "chris")
    result = _run(["profile", "add", "dev", "--server", "other", "--username", "chris"])
    assert result.exit_code == 0
    assert token_store.load_token("dev") is None
    assert "Cleared cached session" in result.stderr

    # identical re-add keeps the session
    config_store.add_profile("dev2", server="sandbox", username="chris")
    token_store.save_token("dev2", AUTH, FUTURE, "chris")
    result = _run(["profile", "add", "dev2", "--server", "sandbox", "--username", "chris"])
    assert result.exit_code == 0
    assert token_store.load_token("dev2") is not None


def test_auth_status_labels_profile_server_not_override(monkeypatch):
    # F3/r3910689367
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    token_store.save_token("dev", AUTH, FUTURE, "chris")
    result = _run(["--server", "other", "auth", "status"])
    assert "dev (sandbox.pamdas.org): valid" in result.output
    assert "other" not in result.output


def test_rotation_not_persisted_if_session_replaced(monkeypatch):
    # F1/r3910689280 — compare-and-swap against the original cache
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    token_store.save_token("dev", AUTH, PAST, "chris")
    fake = FakeER()

    def rotating_auth_headers():
        # another process invalidates the session mid-command
        token_store.delete_token("dev")
        fake.auth = {"access_token": "acc-2", "refresh_token": "r2", "token_type": "Bearer"}
        fake.auth_expires = FUTURE
        return {}

    fake.auth_headers = rotating_auth_headers
    monkeypatch.setattr(cli_mod, "make_token_client", lambda **kw: fake)
    result = _run(["events", "list", "categories"])
    assert result.exit_code == 0
    assert token_store.load_token("dev") is None  # not resurrected


def test_delete_failures_are_loud(monkeypatch):
    # F6+F7/r-suppressed cli:448,500
    import pathlib

    config_store.add_profile("dev", server="sandbox", username="chris")
    token_store.save_token("dev", AUTH, FUTURE, "chris")

    def failing_unlink(self):
        raise PermissionError("read-only fs")

    monkeypatch.setattr(pathlib.Path, "unlink", failing_unlink)
    monkeypatch.setenv("ER_PROFILE", "dev")
    result = _run(["profile", "set", "server", "other"])
    assert result.exit_code == 1
    assert "could not delete cached session" in result.output
    assert config_store.get_profile("dev")["server"] == "sandbox"  # not committed

    result = _run(["profile", "remove", "dev"])
    assert result.exit_code == 1
    assert config_store.get_profile("dev") is not None  # removal not finalized


# --- Copilot round 2 -------------------------------------------------------


def test_http_override_of_https_profile_bypasses_cache(monkeypatch):
    # G1/r3910752034 — same netloc, downgraded scheme: token must stay home
    config_store.add_profile("dev", server="https://x.example.com", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    token_store.save_token("dev", AUTH, FUTURE, "chris")
    captured = {}

    def fake_make_client(*, server, username, password):
        captured.update(server=server, password=password)
        return FakeER()

    monkeypatch.setattr(cli_mod, "make_client", fake_make_client)
    result = _run(
        ["--server", "http://x.example.com", "events", "list", "categories"],
        input="pw\n",
    )
    assert result.exit_code == 0
    assert captured == {"server": "http://x.example.com", "password": "pw"}


def test_auth_login_rejects_scheme_mismatch(monkeypatch):
    # G1/r3910752034 — login must not mint a session via a downgraded scheme
    config_store.add_profile("dev", server="https://x.example.com", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    result = _run(["auth", "login", "--server", "http://x.example.com", "--password", "pw"])
    assert result.exit_code != 0
    out = result.output + result.stderr
    assert "differs from profile" in out
    assert token_store.load_token("dev") is None


def test_profile_add_overwrite_clears_unreadable_session():
    # G3/r3910752085 — invalidation decided from the profiles, not from
    # whether the old token file currently parses
    config_store.add_profile("dev", server="sandbox", username="chris")
    path = token_store.token_file("dev")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{")
    assert token_store.load_token("dev") is None  # unreadable, but present
    result = _run(["profile", "add", "dev", "--server", "other", "--username", "chris"])
    assert result.exit_code == 0
    assert not path.exists()


def test_profile_set_server_clears_unreadable_session(monkeypatch):
    # G4/r3910752105 — a server change always attempts session deletion
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    path = token_store.token_file("dev")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{")
    result = _run(["profile", "set", "server", "other"])
    assert result.exit_code == 0
    assert not path.exists()
    assert config_store.get_profile("dev")["server"] == "other"


def test_profile_set_username_clears_unverifiable_session(monkeypatch):
    # G4/r3910752105 — cached owner can't be verified -> clear it
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    path = token_store.token_file("dev")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{")
    result = _run(["profile", "set", "username", "someone"])
    assert result.exit_code == 0
    assert not path.exists()


# --- Copilot round 3 -------------------------------------------------------


def test_profile_add_invalid_name_is_clean_error():
    # H1/r3915330064 — unsafe names must fail as a ConfigError, not a traceback
    result = _run(["profile", "add", "../evil", "--server", "x", "--username", "u"])
    assert result.exit_code == 1
    assert "error:" in result.output + result.stderr


def test_auth_login_aborts_if_profile_changed_mid_login(monkeypatch):
    # H2/r3915329961 — a profile repointed during the network login must not
    # adopt the token minted for the old server
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")

    class RacingLoginClient(FakeLoginClient):
        def login(self):
            # simulate a concurrent 'er profile set server' winning the race
            config_store.set_profile_property("dev", "server", "other")
            return super().login()

    monkeypatch.setattr(cli_mod, "make_client", lambda **kw: RacingLoginClient())
    result = _run(["auth", "login", "--username", "chris", "--password", "pw"])
    assert result.exit_code == 1
    assert "changed during login" in result.output + result.stderr
    assert token_store.load_token("dev") is None


def test_profile_add_rereads_identity_under_lock(monkeypatch):
    # H4/r-suppressed cli:440 — the invalidation decision must use the profile
    # as it stands once the lock is held, not a pre-lock snapshot
    config_store.add_profile("dev", server="sandbox", username="chris")
    token_store.save_token("dev", AUTH, FUTURE, "chris")
    real_lock = token_store.profile_lock

    from contextlib import contextmanager

    @contextmanager
    def racing_lock(name):
        with real_lock(name):
            # another process repointed and re-logged-in just before we
            # acquired the lock
            config_store.set_profile_property("dev", "server", "other")
            token_store.save_token("dev", {**AUTH, "access_token": "theirs"}, FUTURE, "eve")
            yield

    monkeypatch.setattr(cli_mod.token_store, "profile_lock", racing_lock)
    # identical to the *stale* snapshot — must still invalidate, because the
    # post-lock profile ("other"/"eve") differs from what we're writing
    result = _run(["profile", "add", "dev", "--server", "sandbox", "--username", "chris"])
    assert result.exit_code == 0
    assert token_store.load_token("dev") is None


# --- Copilot round 5 -------------------------------------------------------


def test_rotation_persist_failure_warns_not_crashes(monkeypatch):
    # K1/r3916126247 — _persist_rotation runs after _api_errors returned; a
    # lock/storage failure must warn, not traceback, and not fail the command
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    token_store.save_token("dev", AUTH, PAST, "chris")
    fake = FakeER()

    def rotating_auth_headers():
        fake.auth = {"access_token": "acc-2", "refresh_token": "r2", "token_type": "Bearer"}
        fake.auth_expires = FUTURE
        return {}

    fake.auth_headers = rotating_auth_headers
    monkeypatch.setattr(cli_mod, "make_token_client", lambda **kw: fake)

    real_lock = token_store.profile_lock
    calls = {"n": 0}

    def flaky_lock(name):
        # the connect-time snapshot works; the persist at command close hits
        # a filesystem failure
        calls["n"] += 1
        if calls["n"] > 1:
            raise config_store.ConfigError("could not acquire session lock")
        return real_lock(name)

    monkeypatch.setattr(cli_mod.token_store, "profile_lock", flaky_lock)
    result = _run(["events", "list", "categories"])
    assert result.exit_code == 0  # the command itself succeeded
    assert "warning:" in result.stderr and "rotated session" in result.stderr


def test_profile_add_clears_orphan_token():
    # K2/r3916126172 — a token file with no profile in config is an orphan;
    # creating that profile must not adopt it
    token_store.save_token("dev", AUTH, FUTURE, "chris")
    assert config_store.get_profile("dev") is None
    result = _run(["profile", "add", "dev", "--server", "other", "--username", "eve"])
    assert result.exit_code == 0
    assert token_store.load_token("dev") is None


def test_connect_snapshot_taken_under_lock(monkeypatch):
    # L1/r3916209159 — profile and token must be read as one coherent pair
    # under the profile lock; a replacement session committed just before we
    # acquire it must not be sent to the previously resolved (stale) server
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    token_store.save_token("dev", AUTH, FUTURE, "chris")
    real_lock = token_store.profile_lock

    from contextlib import contextmanager

    @contextmanager
    def racing_lock(name):
        with real_lock(name):
            # a concurrent 'profile set server' + 'auth login' won the race
            config_store.set_profile_property("dev", "server", "other")
            token_store.save_token("dev", {**AUTH, "access_token": "theirs"}, FUTURE, "chris")
            yield

    monkeypatch.setattr(cli_mod.token_store, "profile_lock", racing_lock)
    captured = {}

    def fake_make_client(*, server, username, password):
        captured.update(server=server, password=password)
        return FakeER()

    monkeypatch.setattr(cli_mod, "make_client", fake_make_client)
    result = _run(["events", "list", "categories"], input="pw\n")
    assert result.exit_code == 0
    # the coherent snapshot says the profile now points at "other", which no
    # longer matches the resolved server — fall back to the password path
    assert captured == {"server": "sandbox", "password": "pw"}


def test_auth_status_reports_one_coherent_identity(monkeypatch):
    # L1 suppressed cli:399 — status must describe the profile and session as
    # they stand together under the lock, never a torn old-host/new-session mix
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    token_store.save_token("dev", AUTH, FUTURE, "chris")
    real_lock = token_store.profile_lock

    from contextlib import contextmanager

    @contextmanager
    def racing_lock(name):
        with real_lock(name):
            config_store.set_profile_property("dev", "server", "other")
            token_store.save_token("dev", {**AUTH, "access_token": "theirs"}, FUTURE, "eve")
            yield

    monkeypatch.setattr(cli_mod.token_store, "profile_lock", racing_lock)
    result = _run(["auth", "status"])
    assert result.exit_code == 0
    assert "other.pamdas.org" in result.output  # the session's actual host
    assert "as eve" in result.output


def test_profile_add_traversal_name_cannot_touch_other_sessions():
    # r3916762242 — 'profile add ../tokens/dev' must not acquire dev's lock or
    # delete dev's session on its way to the name-validation error
    config_store.add_profile("dev", server="sandbox", username="chris")
    token_store.save_token("dev", AUTH, FUTURE, "chris")
    result = _run(["profile", "add", "../tokens/dev", "--server", "x", "--username", "u"])
    assert result.exit_code == 1
    assert "error:" in result.output + result.stderr
    assert token_store.load_token("dev") is not None  # untouched


def test_token_flag_uses_static_client_and_needs_no_username(monkeypatch):
    captured = {}

    def fake_static(*, server, token):
        captured.update(server=server, token=token)
        return FakeER()

    monkeypatch.setattr(cli_mod, "make_static_token_client", fake_static)
    monkeypatch.setattr(
        cli_mod, "make_client", lambda **kw: pytest.fail("password client must not be built")
    )
    result = _run(["--server", "sandbox", "--token", "tok-1", "events", "list", "categories"])
    assert result.exit_code == 0
    assert captured == {"server": "sandbox", "token": "tok-1"}


def test_token_env_var_is_honoured_and_accepted_after_subcommand(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        cli_mod,
        "make_static_token_client",
        lambda *, server, token: captured.update(server=server, token=token) or FakeER(),
    )
    monkeypatch.setenv("ER_TOKEN", "tok-env")
    result = _run(["events", "list", "categories", "--server", "sandbox"])
    assert result.exit_code == 0
    assert captured == {"server": "sandbox", "token": "tok-env"}

    monkeypatch.delenv("ER_TOKEN")
    result = _run(["events", "list", "categories", "--server", "sandbox", "--token", "tok-late"])
    assert result.exit_code == 0
    assert captured["token"] == "tok-late"


def test_token_beats_password_and_cached_session(monkeypatch):
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    token_store.save_token("dev", AUTH, FUTURE, "chris")
    captured = {}
    monkeypatch.setattr(
        cli_mod,
        "make_static_token_client",
        lambda *, server, token: captured.update(server=server, token=token) or FakeER(),
    )
    monkeypatch.setattr(
        cli_mod, "make_client", lambda **kw: pytest.fail("password client must not be built")
    )
    monkeypatch.setattr(
        cli_mod, "make_token_client", lambda **kw: pytest.fail("cached session must not be used")
    )
    result = _run(["--password", "pw", "--token", "tok-1", "events", "list", "categories"])
    assert result.exit_code == 0
    assert captured == {"server": "sandbox", "token": "tok-1"}  # server came from the profile


def test_token_without_server_is_usage_error(monkeypatch):
    monkeypatch.delenv("ER_PROFILE", raising=False)
    result = _run(["--token", "tok-1", "events", "list", "categories"])
    assert result.exit_code == 2
    assert "Missing server" in result.output


def _static_fake(token="tok-1"):
    """A FakeER seeded the way erclient seeds a constructor `token=`."""
    fake = FakeER()
    fake.auth = {"token_type": "Bearer", "access_token": token}
    fake.auth_expires = token_store.STATIC_EXPIRES
    return fake


def test_auth_login_with_token_verifies_and_stores_static_record(monkeypatch):
    config_store.add_profile("dev", server="sandbox")
    monkeypatch.setenv("ER_PROFILE", "dev")
    fake = _static_fake()
    monkeypatch.setattr(cli_mod, "make_static_token_client", lambda **kw: fake)
    monkeypatch.setattr(
        cli_mod, "make_client", lambda **kw: pytest.fail("password client must not be built")
    )
    result = _run(["auth", "login", "--token", "tok-1"])
    assert result.exit_code == 0, result.output
    assert ("get_me",) in fake.calls
    assert "Authenticated with a static token" in result.output
    data = token_store.load_token("dev")
    assert data["access_token"] == "tok-1"
    assert data["username"] == "chris"
    assert token_store.is_static(data)
    assert config_store.get_profile("dev")["username"] == "chris"
    assert "Profile 'dev' username set to 'chris'." in result.output


def test_auth_login_with_bad_token_exits_1_and_caches_nothing(monkeypatch):
    from erclient.er_errors import ERClientBadCredentials

    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    fake = _static_fake("bad")

    def failing_get_me():
        raise ERClientBadCredentials("Invalid token.")

    fake.get_me = failing_get_me
    monkeypatch.setattr(cli_mod, "make_static_token_client", lambda **kw: fake)
    result = _run(["auth", "login", "--token", "bad"])
    assert result.exit_code == 1
    assert "error: token rejected by sandbox.pamdas.org: Invalid token." in result.output
    assert token_store.load_token("dev") is None


def test_auth_login_token_outage_is_not_reported_as_rejection(monkeypatch):
    from erclient.er_errors import ERClientException

    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    fake = _static_fake()

    def failing_get_me():
        raise ERClientException("Failed to call ER web service after 6 tries. 503")

    fake.get_me = failing_get_me
    monkeypatch.setattr(cli_mod, "make_static_token_client", lambda **kw: fake)
    result = _run(["auth", "login", "--token", "tok-1"])
    assert result.exit_code == 1
    assert "token rejected" not in result.output
    assert "error: Failed to call ER web service" in result.output
    assert token_store.load_token("dev") is None


def test_auth_login_token_explicit_username_must_match_owner(monkeypatch):
    config_store.add_profile("dev", server="sandbox", username="alice")
    monkeypatch.setenv("ER_PROFILE", "dev")
    monkeypatch.setattr(cli_mod, "make_static_token_client", lambda **kw: _static_fake())
    result = _run(["auth", "login", "--token", "tok-1", "--username", "alice"])
    assert result.exit_code == 2
    assert (
        "token belongs to 'chris', not 'alice' (from --username / ER_USERNAME); "
        "pass --username chris or unset ER_USERNAME."
    ) in result.output
    assert token_store.load_token("dev") is None
    assert config_store.get_profile("dev")["username"] == "alice"


def test_auth_login_token_env_username_is_also_an_identity_claim(monkeypatch):
    # consistent with _connect, which refuses to ride a cached session on a
    # username that came from ER_USERNAME; the message names the env var
    config_store.add_profile("dev", server="sandbox")
    monkeypatch.setenv("ER_PROFILE", "dev")
    monkeypatch.setenv("ER_USERNAME", "alice")
    monkeypatch.setattr(cli_mod, "make_static_token_client", lambda **kw: _static_fake())
    result = _run(["auth", "login", "--token", "tok-1"])
    assert result.exit_code == 2
    assert "unset ER_USERNAME" in result.output
    assert token_store.load_token("dev") is None


def test_auth_login_token_updates_profile_default_username_to_owner(monkeypatch):
    # a profile *default* username is not an explicit identity claim: like
    # password login, the profile follows whoever the credential belongs to
    config_store.add_profile("dev", server="sandbox", username="alice")
    monkeypatch.setenv("ER_PROFILE", "dev")
    monkeypatch.setattr(cli_mod, "make_static_token_client", lambda **kw: _static_fake())
    result = _run(["auth", "login", "--token", "tok-1"])
    assert result.exit_code == 0, result.output
    assert "Profile 'dev' username set to 'chris'." in result.output
    assert config_store.get_profile("dev")["username"] == "chris"


def test_revoked_static_token_mid_command_names_profile_and_remedy(monkeypatch):
    from erclient.er_errors import ERClientBadCredentials

    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    _store_static()
    fake = FakeER()

    def revoked(include_inactive=False):
        raise ERClientBadCredentials("Invalid token.")

    fake.get_event_categories = revoked
    monkeypatch.setattr(cli_mod, "make_token_client", lambda **kw: fake)
    result = _run(["events", "list", "categories"])
    assert result.exit_code == 1
    assert (
        "error: credentials rejected (Invalid token.) — the credential stored on profile "
        "'dev' is expired or revoked; run 'er auth login' (or 'er auth login --token')."
    ) in result.output


def test_bad_token_flag_mid_command_points_at_the_flag(monkeypatch):
    from erclient.er_errors import ERClientBadCredentials

    fake = FakeER()

    def revoked(include_inactive=False):
        raise ERClientBadCredentials("Invalid token.")

    fake.get_event_categories = revoked
    monkeypatch.setattr(cli_mod, "make_static_token_client", lambda **kw: fake)
    result = _run(["--server", "sandbox", "--token", "bad", "events", "list", "categories"])
    assert result.exit_code == 1
    assert (
        "error: credentials rejected (Invalid token.) — check --token / ER_TOKEN." in result.output
    )


def test_auth_login_token_still_requires_matching_server(monkeypatch):
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    monkeypatch.setattr(
        cli_mod, "make_static_token_client", lambda **kw: pytest.fail("must not reach network")
    )
    result = _run(["auth", "login", "--token", "tok-1", "--server", "other"])
    assert result.exit_code == 2
    assert "differs from profile 'dev'" in result.output


def _store_static(name="dev", token="tok-1", username="chris"):
    token_store.save_token(
        name, {"access_token": token}, token_store.STATIC_EXPIRES, username, static=True
    )


def test_connect_uses_stored_static_token_without_refresh_or_rotation(monkeypatch):
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    _store_static()
    fake = FakeER()
    monkeypatch.setattr(cli_mod, "make_token_client", lambda **kw: fake)
    monkeypatch.setattr(
        cli_mod, "make_client", lambda **kw: pytest.fail("password client must not be built")
    )
    result = _run(["events", "list", "categories"])
    assert result.exit_code == 0
    assert fake.auth == {"access_token": "tok-1", "refresh_token": "", "token_type": "Bearer"}
    assert fake.auth_expires.year == 2099
    # nothing rotated, so the record on disk is byte-for-byte what we stored
    data = token_store.load_token("dev")
    assert data["access_token"] == "tok-1" and token_store.is_static(data)


def test_status_show_and_list_report_static_token(monkeypatch):
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    _store_static()

    result = _run(["auth", "status"])
    assert result.exit_code == 0
    assert (
        result.output.strip() == "dev (sandbox.pamdas.org): static token as chris (never refreshes)"
    )

    result = _run(["profile", "show"])
    assert "auth:      static token as chris (never refreshes)" in result.output

    result = _run(["profile", "list"])
    assert result.output.rstrip().endswith("static token")


def test_profile_set_username_to_other_user_clears_static_token(monkeypatch):
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    _store_static()
    result = _run(["profile", "set", "username", "alice"])
    assert result.exit_code == 0
    assert "Cleared cached session for profile 'dev'" in result.output
    assert token_store.load_token("dev") is None
