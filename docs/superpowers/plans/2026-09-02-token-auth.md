# Bearer-token auth (`--token` / `ER_TOKEN`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `er` authenticate with a pre-issued OAuth bearer token — per invocation via `--token`/`ER_TOKEN`, or stored on a profile via `er auth login --token` — so agent sandboxes never need an interactive password login.

**Architecture:** A static token is just a session record with no refresh token and a far-future expiry, so it rides the existing `token_store` → `apply_to_client` → `auth_headers` path unchanged; erclient already treats a constructor `token=` the same way (expires 2099). `_connect` gains one new branch at the top of its precedence chain (explicit token), `auth login` gains a token branch that verifies the token against `/user/me/` and stores it flagged `static`, and the three status displays learn to say "static token". No new modules.

**Tech Stack:** Python 3.11, click, earthranger-client (`ERClient(token=...)`, `get_me()`), pytest with the existing `FakeER` fake and `CliRunner`.

**Spec:** `docs/superpowers/specs/2026-09-02-er-cli-parity-design.md` §"Bearer-token auth" (P0 item 2).

## Global Constraints

- Python ≥3.11; deps stay `earthranger-client>=1.16.0`, `click>=8.1`, `pyyaml>=6.0` (no additions).
- Secrets never go in `config.json`; tokens live only under `<config>/tokens/<profile>.json` (0600).
- Every session mutation happens under `token_store.profile_lock(name)`.
- Errors print `error: ...` and exit 1 via `_api_errors`; usage mistakes raise `click.UsageError`.
- Tests are offline: never construct a real network call; monkeypatch `make_client` / `make_token_client` / `make_static_token_client` in `earthranger_cli.cli`.
- Run the full suite with `uv run pytest -q` before each commit; `uv run ruff check src tests` must be clean.

## Decisions locked in this plan

**Precedence in `_connect`** (settles the spec's open question):

1. explicit `--token` / `ER_TOKEN` → static-token client on the resolved server; username ignored
2. explicit `--password` / `ER_PASSWORD` → password client
3. selected profile's stored record (a static token or a cached login session — one record per profile, whichever was stored last), subject to the existing server-match and username-match checks
4. interactive password prompt

**Storing a token on a profile** is done by `er auth login --token T` (parallel to password login: it verifies and caches). The spec's `profile add --token` / `profile set token` are dropped for now — `auth login --token` covers the need without putting a network call inside `profile add`. Note this substitution in the spec when closing the item.

**Verification.** `auth login --token` calls `client.get_me()` and stores the returned `username`, so the existing username-match logic and `profile set username` invalidation work unchanged for static records. The per-invocation `--token` path does no eager check (a static token can only be checked by making a request); a bad token surfaces as the API's 401 on the first call.

---

### Task 1: `token_store` learns static records

**Files:**
- Modify: `src/earthranger_cli/token_store.py:93-108` (`save_token`), add `is_static`
- Test: `tests/test_token_store.py`

**Interfaces:**
- Produces: `save_token(profile, auth, expires_at, username, *, static: bool = False) -> None` — writes `"static": true` into the record when set.
- Produces: `is_static(data: dict) -> bool`.
- Produces: `STATIC_EXPIRES: datetime = datetime(2099, 1, 1, tzinfo=UTC)` — the expiry used for static records (matches erclient's own `token=` handling).
- `load_token` returns the record including `static`; no signature change.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_token_store.py`:

```python
def test_save_static_token_flags_record_and_never_expires():
    token_store.save_token(
        "dev", {"access_token": "tok-1"}, token_store.STATIC_EXPIRES, "chris", static=True
    )
    data = token_store.load_token("dev")
    assert data["access_token"] == "tok-1"
    assert data["refresh_token"] == ""
    assert data["token_type"] == "Bearer"
    assert data["username"] == "chris"
    assert token_store.is_static(data) is True
    assert token_store.is_expired(data) is False


def test_login_session_is_not_static():
    token_store.save_token("dev", AUTH, EXPIRES, "chris")
    data = token_store.load_token("dev")
    assert token_store.is_static(data) is False
    assert "static" not in data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_token_store.py -q -k "static"`
Expected: FAIL — `AttributeError: module 'earthranger_cli.token_store' has no attribute 'STATIC_EXPIRES'`

- [ ] **Step 3: Implement**

In `src/earthranger_cli/token_store.py`, add after the imports:

```python
# Static (pre-issued) bearer tokens have no refresh token; we give them the
# same nominal expiry erclient assigns to a constructor `token=` so the
# existing "is it valid?" check never triggers a refresh attempt.
STATIC_EXPIRES = datetime(2099, 1, 1, tzinfo=UTC)
```

Replace `save_token`:

```python
def save_token(
    profile: str, auth: dict, expires_at: datetime, username: str, *, static: bool = False
) -> None:
    path = token_file(profile)
    payload = {
        "access_token": auth["access_token"],
        "refresh_token": auth.get("refresh_token") or "",
        "token_type": auth.get("token_type") or "Bearer",
        "expires_at": expires_at.isoformat(),
        "username": username,
        # scope marker: binds the record to this profile, so a legacy
        # host-keyed cache file (or a copied record) is never adopted
        "profile": profile,
    }
    if static:
        # a pre-issued bearer token: no refresh token, never rotated, and
        # reported as such by `auth status`
        payload["static"] = True
    write_private(path, json.dumps(payload, indent=2))
```

Add after `is_expired`:

```python
def is_static(data: dict) -> bool:
    return bool(data.get("static"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_token_store.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/earthranger_cli/token_store.py tests/test_token_store.py
git commit -m "token_store: flag static bearer-token records"
```

---

### Task 2: `client.make_static_token_client`

**Files:**
- Modify: `src/earthranger_cli/client.py:24-40`
- Test: `tests/test_client.py`

**Interfaces:**
- Produces: `make_static_token_client(*, server: str, token: str) -> ERClient` — an `ERClient` whose `auth` is `{"token_type": "Bearer", "access_token": token}` and `auth_expires` is 2099-01-01 UTC (erclient sets both from its `token=` kwarg). `client_id` is still set so nothing downstream sees `None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_client.py` (add `make_static_token_client` to the import list at the top):

```python
def test_make_static_token_client_preloads_bearer_and_never_refreshes():
    client = make_static_token_client(server="myreserve", token="tok-1")
    assert client.service_root == "https://myreserve.pamdas.org"
    assert client.auth == {"token_type": "Bearer", "access_token": "tok-1"}
    assert client.auth_expires.year == 2099
    assert client.username is None and client.password is None
    # no network: auth_headers must not try to log in
    assert client.auth_headers()["Authorization"] == "Bearer tok-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_client.py -q -k static`
Expected: FAIL — `ImportError: cannot import name 'make_static_token_client'`

- [ ] **Step 3: Implement**

Add to `src/earthranger_cli/client.py` after `make_token_client`:

```python
def make_static_token_client(*, server: str, token: str) -> ERClient:
    """Client authenticated with a pre-issued bearer token (--token / ER_TOKEN).

    erclient's `token=` kwarg preloads `auth` and sets `auth_expires` to 2099,
    so auth_headers() never attempts a refresh or password login; an invalid
    token surfaces as the API's 401 on the first request.
    """
    return ERClient(
        service_root=normalize_server(server),
        token=token,
        client_id=DEFAULT_CLIENT_ID,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_client.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/earthranger_cli/client.py tests/test_client.py
git commit -m "client: add make_static_token_client for pre-issued bearer tokens"
```

---

### Task 3: `--token` / `ER_TOKEN` on every command, highest precedence

**Files:**
- Modify: `src/earthranger_cli/cli.py:18` (import), `:44-68` (`connection_options`), `:91-118` (`_connect`), `:175-185` (root group)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `make_static_token_client(*, server, token)` from Task 2.
- Produces: `ctx.obj["token"]` populated from `--token` (root or trailing) or `ER_TOKEN`; `_connect(ctx)` returns a static-token client when it is set.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py` (after the existing `_connect` tests; `config_store`, `token_store`, `AUTH`, `FUTURE`, `FakeER`, `_run` and `cli_mod` are already imported/defined there):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q -k token_`
Expected: FAIL — `AttributeError: <module 'earthranger_cli.cli'> has no attribute 'make_static_token_client'` and `Error: No such option: --token`

- [ ] **Step 3: Implement**

In `src/earthranger_cli/cli.py`:

Change the import on line 18 to:

```python
from .client import make_client, make_static_token_client, make_token_client, normalize_server
```

In `connection_options`, extend the wrapper's keyword parameters and the loop, and add the option:

```python
    def wrapper(
        *args,
        server_=None,
        username_=None,
        password_=None,
        token_=None,
        profile_=None,
        **kwargs,
    ):
        ctx = click.get_current_context()
        for key, val in (
            ("server", server_),
            ("username", username_),
            ("password", password_),
            ("token", token_),
            ("profile", profile_),
        ):
            if val:
                ctx.obj[key] = val
        return f(*args, **kwargs)

    wrapper = functools.update_wrapper(wrapper, f)
    for opt in (
        click.option("--profile", "profile_", help="Named profile to use."),
        click.option("--token", "token_", help="Pre-issued OAuth bearer token."),
        click.option("--password", "password_", help="EarthRanger password."),
        click.option("--username", "username_", help="EarthRanger username."),
        click.option("--server", "server_", help="ER site name or full https:// URL."),
    ):
        wrapper = opt(wrapper)
    return wrapper
```

In `_connect`, add the token branch immediately after `server, username = _resolve_connection(ctx)` and update the docstring:

```python
def _connect(ctx):
    """Build an authenticated client.

    Precedence: an explicit bearer token (--token or ER_TOKEN) wins; else an
    explicit password (flag or ER_PASSWORD); else the selected profile's
    stored record (a static token from `auth login --token` or a session
    cached by `auth login`); else an interactive password prompt.
    """
    server, username = _resolve_connection(ctx)
    token = ctx.obj.get("token")
    if token:
        # the token is the identity: no username, no cache, no refresh
        return make_static_token_client(server=server, token=token)
    password = ctx.obj["password"]
```

(The rest of `_connect` is unchanged.)

On the root group, add the option and store it:

```python
@click.group()
@click.option("--server", envvar="ER_SERVER", help="ER site name (myreserve) or full https:// URL.")
@click.option("--username", envvar="ER_USERNAME", help="EarthRanger username.")
@click.option(
    "--password", envvar="ER_PASSWORD", help="EarthRanger password (prompted if omitted)."
)
@click.option(
    "--token",
    envvar="ER_TOKEN",
    help="Pre-issued OAuth bearer token (wins over --password and cached sessions).",
)
@click.option("--profile", envvar="ER_PROFILE", help="Named profile to use (see 'er profile').")
@click.pass_context
def main(ctx, server, username, password, token, profile):
    """EarthRanger site management CLI."""
    ctx.obj = {
        "server": server,
        "username": username,
        "password": password,
        "token": token,
        "profile": profile,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q`
Expected: all PASS (the two `test_connection_flags_accepted_after_subcommand` / `test_trailing_flags_override_globals` tests exercise `connection_options` and must still pass).

- [ ] **Step 5: Commit**

```bash
git add src/earthranger_cli/cli.py tests/test_cli.py
git commit -m "cli: accept --token / ER_TOKEN as the highest-precedence credential"
```

---

### Task 4: `er auth login --token` stores a verified static token on the profile

**Files:**
- Modify: `src/earthranger_cli/cli.py:333-372` (`auth_login`)
- Modify: `tests/conftest.py` (add `get_me` to `FakeER`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `make_static_token_client` (Task 2), `token_store.save_token(..., static=True)` and `token_store.STATIC_EXPIRES` (Task 1).
- Consumes: `ERClient.get_me() -> dict` (erclient; returns the `/user/me/` record, which has a `username` key).
- Produces: a static record on the selected profile with `username` taken from `get_me()`; the profile's `username` property is updated to match, exactly as password login does.

- [ ] **Step 1: Add `get_me` to the fake**

In `tests/conftest.py`, add to `FakeER.__init__`:

```python
        self.me = {"username": "chris", "id": "user-1"}
```

and add a method next to `auth_headers`:

```python
    def get_me(self):
        self.calls.append(("get_me",))
        return self.me
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_auth_login_with_token_verifies_and_stores_static_record(monkeypatch):
    config_store.add_profile("dev", server="sandbox")
    monkeypatch.setenv("ER_PROFILE", "dev")
    fake = FakeER()
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
    from erclient.er_errors import ERClientException

    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    fake = FakeER()

    def failing_get_me():
        raise ERClientException("401 Unauthorized")

    fake.get_me = failing_get_me
    monkeypatch.setattr(cli_mod, "make_static_token_client", lambda **kw: fake)
    result = _run(["auth", "login", "--token", "bad"])
    assert result.exit_code == 1
    assert "error: token rejected by sandbox.pamdas.org" in result.output
    assert token_store.load_token("dev") is None


def test_auth_login_token_still_requires_matching_server(monkeypatch):
    config_store.add_profile("dev", server="sandbox", username="chris")
    monkeypatch.setenv("ER_PROFILE", "dev")
    monkeypatch.setattr(
        cli_mod, "make_static_token_client", lambda **kw: pytest.fail("must not reach network")
    )
    result = _run(["auth", "login", "--token", "tok-1", "--server", "other"])
    assert result.exit_code == 2
    assert "differs from profile 'dev'" in result.output
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q -k "auth_login_with or auth_login_token"`
Expected: the first two FAIL (login demands a username / password prompt instead of using the token); the third already PASSES because the server check precedes credentials — keep it as a regression guard.

- [ ] **Step 4: Implement**

Replace the body of `auth_login` from `password = ctx.obj["password"]` through the end of the function with:

```python
    host = token_store.server_host(server)
    token = ctx.obj.get("token")
    if token:
        # a pre-issued bearer token: verify it against /user/me/ so a typo
        # fails here rather than on the first real command, and learn the
        # owner so the profile's identity and the username-match rule work
        client = make_static_token_client(server=server, token=token)
        try:
            username = client.get_me()["username"]
        except (ERClientException, KeyError, TypeError) as e:
            click.echo(f"error: token rejected by {host}: {e}")
            sys.exit(1)
        auth = {"access_token": token, "token_type": "Bearer"}
        expires_at = token_store.STATIC_EXPIRES
        static = True
    else:
        password = ctx.obj["password"]
        if not username:
            raise click.UsageError("Missing username: pass --username or set ER_USERNAME.")
        if not password:
            password = click.prompt("Password", hide_input=True)
        client = make_client(server=server, username=username, password=password)
        if not client.login():
            click.echo(f"error: login failed for {username!r} at {host}")
            sys.exit(1)
        auth = client.auth
        expires_at = client.auth_expires
        static = False
    with token_store.profile_lock(name):
        # the token was minted for the profile as it stood before the network
        # round-trip; if a concurrent command repointed it since, this session
        # belongs to the old identity and must not be cached
        if config_store.get_profile(name) != profile:
            click.echo(
                f"error: profile {name!r} changed during login — session not cached; "
                "re-run 'er auth login'."
            )
            sys.exit(1)
        token_store.save_token(name, auth, expires_at, username, static=static)
        if profile.get("username") != username:
            # the profile's identity follows whoever actually logged in
            config_store.set_profile_property(name, "username", username)
            click.echo(f"Profile {name!r} username set to {username!r}.")
    if static:
        click.echo(
            f"Authenticated with a static token. Stored on profile {name!r} ({host}); "
            "it will not be refreshed — re-run 'er auth login --token' when it expires."
        )
    else:
        click.echo(f"Authenticated. Session cached on profile {name!r} ({host}).")
```

Also update the `auth login` docstring to: `"""Log in (password, or --token) and cache the credential on the selected profile."""`

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest -q`
Expected: all PASS (the existing `test_auth_login_*` tests cover the password branch, which is unchanged in behaviour).

- [ ] **Step 6: Commit**

```bash
git add src/earthranger_cli/cli.py tests/conftest.py tests/test_cli.py
git commit -m "auth login --token: verify a bearer token via /user/me/ and store it on the profile"
```

---

### Task 5: Static records flow through `_connect` and read as "static token" in status displays

**Files:**
- Modify: `src/earthranger_cli/cli.py:126-135` (`_connect_with_cached_token` error text), `:400-415` (`auth_status`), `:450-453` (`_auth_state`), `:588-616` (`profile_show`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `token_store.is_static(data)` (Task 1).
- Produces: `_auth_state(data)` returns `"static token"` for static records; `auth status` and `profile show` print `static token as <user> (never refreshes)`; `profile list` shows `static token` in its last column.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
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
    assert result.output.strip() == "dev (sandbox.pamdas.org): static token as chris (never refreshes)"

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q -k "static_token or stored_static"`
Expected: `test_connect_uses_stored_static_token_without_refresh_or_rotation` and `test_profile_set_username_to_other_user_clears_static_token` PASS already (the storage path is generic); `test_status_show_and_list_report_static_token` FAILS with `valid as chris (access token expires 2099-01-01...)` in the output.

- [ ] **Step 3: Implement**

In `src/earthranger_cli/cli.py`, replace `_auth_state`:

```python
def _auth_state(data: dict | None) -> str:
    if not data:
        return "not authenticated"
    if token_store.is_static(data):
        return "static token"
    return "expired" if token_store.is_expired(data) else "valid"


def _auth_detail(data: dict | None) -> str:
    """Long form for `auth status` / `profile show`: state, owner, expiry."""
    if not data:
        return "not authenticated"
    as_user = f" as {data['username']}" if data.get("username") else ""
    if token_store.is_static(data):
        return f"static token{as_user} (never refreshes)"
    return f"{_auth_state(data)}{as_user} (access token expires {data['expires_at']})"
```

In `auth_status`, replace everything after `host = ...` with:

```python
    click.echo(f"{name} ({host}): {_auth_detail(data)}")
```

In `profile_show`, delete the `if not data: ... else: ...` block that builds `auth` and change the last line to:

```python
    click.echo(f"auth:      {_auth_detail(data)}")
```

In `_connect_with_cached_token`, make the expiry message accurate for both record kinds:

```python
    except ERClientException as e:
        kind = "static token" if token_store.is_static(cached) else "cached session"
        raise ERClientException(
            f"{kind} for profile {name!r} expired or invalid — run 'er auth login'"
        ) from e
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all PASS, ruff clean. `test_auth_status_labels_profile_server_not_override` and `test_profile_show_by_name` exercise the refactored display and must still pass unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/earthranger_cli/cli.py tests/test_cli.py
git commit -m "auth status / profile show|list: report static tokens as such"
```

---

### Task 6: Docs — README, spec checkbox

**Files:**
- Modify: `README.md:83-97` (the "You can always bypass the cache" paragraph and the expiry paragraph), `README.md:151-163` (Commands table `auth` row)
- Modify: `docs/superpowers/specs/2026-09-02-er-cli-parity-design.md` (§Bearer-token auth, P0 todo)

- [ ] **Step 1: README — replace the bypass paragraph**

Replace the paragraph starting `You can always bypass the cache with an explicit password` and its code block with:

````markdown
You can always bypass the cache with an explicit credential. A pre-issued
OAuth bearer token wins over everything else and needs no username — this is
the path for agent sandboxes and CI, where there is no one to type a
password:

```bash
export ER_SERVER=myreserve
export ER_TOKEN='...'              # single-quote it — tokens can contain $, !, & etc.
er events list categories          # or: er --token '...' events list categories
```

To keep a token on a profile instead, `er auth login --token '...'` verifies
it against the server, records its owner, and stores it in place of a
password session; `er auth status` then reports `static token as <user>
(never refreshes)` — re-run `auth login --token` when the token expires.

An explicit password is next in precedence:

```bash
export ER_USERNAME=me
export ER_PASSWORD=...              # or --password; omit both to be prompted
```

> **Docker:** tokens often contain shell-special characters, so `-e ER_TOKEN=$TOKEN`
> can corrupt the value. Prefer an env-file (`docker run --env-file er.env ...`
> with `ER_SERVER=...` and `ER_TOKEN=...` lines) or `export ER_TOKEN='...'` once
> and forward it by name with a bare `-e ER_TOKEN`.
````

- [ ] **Step 2: README — Commands table**

Change the `auth` row to:

```markdown
| `auth login [--token T]/status/logout` | Cache (password session or static token), inspect, or clear the selected profile's credential |
```

- [ ] **Step 3: Spec — record the decision and tick the item**

In `docs/superpowers/specs/2026-09-02-er-cli-parity-design.md`, under §"Bearer-token auth", replace the paragraph beginning `**Open question — precedence.**` through `so `auth status` should say so.` with:

```markdown
**Decided 2026-09-02** (implemented in `docs/superpowers/plans/2026-09-02-token-auth.md`):

1. explicit `--token` / `ER_TOKEN`
2. explicit `--password` / `ER_PASSWORD`
3. the selected profile's stored record — a static token from `auth login
   --token` or a login session from `auth login` (one record per profile)
4. interactive prompt

`profile add --token` / `profile set token` were dropped: `er auth login
--token` stores a token on the selected profile after verifying it against
`/user/me/`, which keeps `profile add` network-free and gives the record an
owner so the existing username-match rule applies. Static records never
refresh; `auth status` says so.
```

And in the P0 todo, change the second item to:

```markdown
- [x] Bearer-token auth path (`--token`, `ER_TOKEN`, `auth login --token`);
      precedence decided — see §Bearer-token auth.
```

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all PASS, ruff clean.

```bash
git add README.md docs/superpowers/specs/2026-09-02-er-cli-parity-design.md
git commit -m "docs: document --token / ER_TOKEN and auth login --token; close the precedence question"
```

---

## Self-review

- **Spec coverage.** `--token`/`ER_TOKEN` (Task 3), token on a profile (Task 4, via `auth login --token` — substitution recorded in Task 6), precedence decision (Task 3 + Task 6), "auth status should say so" (Task 5), Docker env-file tip (Task 6). `profile add --token` / `profile set token` intentionally not built; spec updated to say why.
- **Placeholders.** None; every code step carries the code.
- **Type consistency.** `make_static_token_client(*, server, token)` is defined in Task 2 and consumed with keyword args in Tasks 3–4; `save_token(..., static=True)` / `STATIC_EXPIRES` / `is_static` are defined in Task 1 and used in Tasks 4–5; `_auth_detail` is defined and used only in Task 5; `FakeER.get_me` is added in Task 4 before the test that needs it.
