# Read surface (P0 of er-cli parity) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `er` the agent-facing read surface of tusker's `packages/er-cli` — read-only `<resource> <action>` commands that auto-paginate and emit er-cli's exact `{"records": [...], "meta": {...}}` JSON to stdout or `-o PATH` — so tusker can depend on this package and retire its own `er`.

**Architecture:** Two small pure modules do the work: `read.py` turns one erclient `_get` call into `(records, meta)` by unwrapping DRF `results/next` pages and following `next` until exhausted or `--limit`; `output.py` writes the `{records, meta}` document. A declarative table in `read_commands.py` (one `ReadCommand` row per endpoint, hand-written flags) is compiled into click commands at import time and registered onto the existing `main` group through injected dependencies (`_connect`, `connection_options`, `_api_errors`) so there is no circular import and the test suite's `monkeypatch.setattr(cli_mod, "_connect", ...)` keeps working. Existing `events list ...` / `events show event-type` gain opt-in `--json` / `-o` using the same two modules. Authoring commands are untouched.

**Tech Stack:** Python 3.11, click 8, earthranger-client 1.16 (`ERClient._get(path, base_url=, params=, max_retries=)`, `ERClient._api_root(version)`), pytest with the existing `FakeER` fake and `CliRunner`.

**Spec:** `docs/superpowers/specs/2026-09-02-er-cli-parity-design.md` — §Output contract, §Read-only resource commands, §Pagination and retries, §Naming; Todo → P0 items 1, 3, 4.

## Global Constraints

- Python ≥3.11; dependencies stay exactly `earthranger-client>=1.16.0`, `click>=8.1`, `pyyaml>=6.0` — no additions (no httpx, no bundled OpenAPI spec).
- JSON output shape is er-cli's, byte-for-byte in structure: `{"records": [...], "meta": {"total": N, "pages": P, "count_reported": C?}}`; `records` is always a flat list, even for single-object `get` commands (one element).
- `-o PATH` creates parent directories and prints exactly one line to **stderr**: `Done. N record(s) written to PATH (P page(s)).` Nothing goes to stdout in that case.
- Default `page_size` for paginated reads is `100` unless the user passes `--page-size`.
- All new reads call `client._get(..., max_retries=0)` (fail fast, like the choices helpers). Retries with backoff are a P1 item — do not add them here.
- Reads are GET only. No POST/PATCH anywhere in this plan.
- Errors print `error: ...` and exit 1 via the existing `_api_errors`; usage mistakes raise `click.UsageError`.
- Every ER `{"data": ..., "status": ...}` envelope is already stripped by erclient's `_get` (it returns `data`), so `read.py` must **not** look for a `data` key.
- Flag naming: primary spelling is click-idiomatic dashed (`--updated-since`); every multi-word flag also accepts er-cli's underscored spelling (`--updated_since`) so tusker skill drafts run unchanged.
- Naming (spec §Naming): event objects stay nested under the existing `events` group (`er events search`, `er events get ID`); everything else is flat `er <resource> <action>` exactly as er-cli names them (`subjects search`, `subject-groups list`, ...). No flat `event-types list` alias in this plan (P1).
- `--event-type` on `events search` passes the value straight to ER (ER filters on event-type **id**). Name→id resolution is P1; the flag help must say "event type id(s)".
- Tests are offline: never construct a real network call. Fakes go through `conftest.FakeER` or `unittest.mock.Mock`.
- Installed click is 8.5: `CliRunner()` takes no `mix_stderr` and always captures stderr separately — assert on `result.stdout` / `result.stderr`; `result.output` is both streams combined.
- Before every commit: `uv run pytest -q` all green and `uv run ruff check src tests` clean. Line length 100.

---

## File map

| File | Responsibility |
|---|---|
| Create `src/earthranger_cli/read.py` | Pure helpers: `normalize_page`, `follow_pages`, `fetch`. No click, no I/O besides `client._get`. |
| Create `src/earthranger_cli/output.py` | `emit(records, meta, output)` — the `{records, meta}` writer. |
| Create `src/earthranger_cli/read_commands.py` | `Flag`, `ReadCommand`, `Deps`, the `COMMANDS` table, `register(main, deps)`. |
| Modify `src/earthranger_cli/client.py:151-165` | `_collect_pages` becomes a thin wrapper over `read.follow_pages`. |
| Modify `src/earthranger_cli/cli.py` | Import + register read commands at the bottom; `--json`/`-o` on the three existing read commands. |
| Modify `tests/conftest.py` | `FakeER.responses` (path → canned page(s)) and `FakeER._api_root`. |
| Create `tests/test_read.py`, `tests/test_output.py`, `tests/test_read_commands.py` | Offline tests per module. |
| Modify `tests/test_cli.py` | `--json` tests for the existing three read commands. |
| Modify `README.md`, `docs/superpowers/specs/2026-09-02-er-cli-parity-design.md`, `src/earthranger_cli/__init__.py` | Docs, tick P0 boxes, bump to 0.2.0. |

---

### Task 1: `read.py` — page normalization, following `next`, and `fetch`

**Files:**
- Create: `src/earthranger_cli/read.py`
- Test: `tests/test_read.py`

**Interfaces:**
- Consumes: any object with `_get(path, base_url=None, params=None, max_retries=0)` (erclient's `ERClient` or `FakeER`/`Mock`) and `_api_root(version) -> str`.
- Produces:
  - `normalize_page(data) -> tuple[list, str | None, int | None]` — `(records, next_url, count)`.
  - `follow_pages(client, page, *, limit=None) -> tuple[list, int, int | None]` — `(records, pages, count)`; follows `next` via `client._get(next_url, max_retries=0)`.
  - `fetch(client, path, params=None, *, paginate=False, limit=None, version=None) -> tuple[list, dict]` — `(records, meta)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_read.py
from unittest.mock import Mock

from earthranger_cli.read import fetch, follow_pages, normalize_page


def test_normalize_drf_page():
    recs, nxt, cnt = normalize_page(
        {"count": 2, "next": "https://x/next", "previous": None, "results": [{"id": 1}, {"id": 2}]}
    )
    assert [r["id"] for r in recs] == [1, 2]
    assert nxt == "https://x/next"
    assert cnt == 2


def test_normalize_plain_list():
    recs, nxt, cnt = normalize_page([{"id": 1}, {"id": 2}])
    assert len(recs) == 2 and nxt is None and cnt == 2


def test_normalize_single_object_becomes_one_record():
    recs, nxt, cnt = normalize_page({"server_time": "2026-09-23T00:00:00Z"})
    assert recs == [{"server_time": "2026-09-23T00:00:00Z"}]
    assert nxt is None and cnt == 1


def test_normalize_none_is_empty():
    assert normalize_page(None) == ([], None, 0)


def test_follow_pages_walks_next_and_counts_pages():
    client = Mock()
    client._get.side_effect = [{"count": 3, "next": None, "results": [{"id": "c"}]}]
    first = {"count": 3, "next": "https://x/subjects/?page=2", "results": [{"id": "a"}, {"id": "b"}]}
    recs, pages, count = follow_pages(client, first)
    assert [r["id"] for r in recs] == ["a", "b", "c"]
    assert pages == 2 and count == 3
    client._get.assert_called_once_with("https://x/subjects/?page=2", max_retries=0)


def test_follow_pages_stops_at_limit_without_extra_requests():
    client = Mock()
    first = {"count": 5, "next": "https://x/?page=2", "results": [{"id": "a"}, {"id": "b"}]}
    recs, pages, _ = follow_pages(client, first, limit=2)
    assert [r["id"] for r in recs] == ["a", "b"]
    assert pages == 1
    client._get.assert_not_called()


def test_follow_pages_trims_overshoot_to_limit():
    client = Mock()
    client._get.side_effect = [{"count": 4, "next": None, "results": [{"id": "c"}, {"id": "d"}]}]
    first = {"count": 4, "next": "https://x/?page=2", "results": [{"id": "a"}, {"id": "b"}]}
    recs, pages, _ = follow_pages(client, first, limit=3)
    assert [r["id"] for r in recs] == ["a", "b", "c"]
    assert pages == 2


def test_fetch_list_adds_default_page_size_and_drops_none_params():
    client = Mock()
    client._get.return_value = {"count": 1, "next": None, "results": [{"id": "a"}]}
    records, meta = fetch(client, "subjects", {"name": "Najin", "bbox": None}, paginate=True)
    assert records == [{"id": "a"}]
    assert meta == {"total": 1, "pages": 1, "count_reported": 1}
    client._get.assert_called_once_with(
        "subjects", base_url=None, params={"name": "Najin", "page_size": 100}, max_retries=0
    )


def test_fetch_respects_explicit_page_size():
    client = Mock()
    client._get.return_value = {"count": 0, "next": None, "results": []}
    fetch(client, "subjects", {"page_size": 5}, paginate=True)
    assert client._get.call_args.kwargs["params"] == {"page_size": 5}


def test_fetch_get_does_not_paginate_or_add_page_size():
    client = Mock()
    client._get.return_value = {"id": "s1", "name": "Najin"}
    records, meta = fetch(client, "subject/s1", paginate=False)
    assert records == [{"id": "s1", "name": "Najin"}]
    assert meta == {"total": 1, "pages": 1, "count_reported": 1}
    client._get.assert_called_once_with("subject/s1", base_url=None, params={}, max_retries=0)


def test_fetch_version_uses_client_api_root():
    client = Mock()
    client._api_root.return_value = "https://x.pamdas.org/api/v2.0"
    client._get.return_value = {"type": "FeatureCollection", "features": []}
    fetch(client, "subject/s1/tracks", {"since": "2026-01-01"}, paginate=False, version="v2.0")
    client._api_root.assert_called_once_with("v2.0")
    assert client._get.call_args.kwargs["base_url"] == "https://x.pamdas.org/api/v2.0"


def test_fetch_meta_omits_count_reported_when_unknown():
    client = Mock()
    client._get.return_value = {"results": [{"id": "a"}], "next": None}  # no "count"
    _, meta = fetch(client, "choices", paginate=True)
    assert meta == {"total": 1, "pages": 1}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_read.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'earthranger_cli.read'`

- [ ] **Step 3: Write the implementation**

```python
# src/earthranger_cli/read.py
"""Read helpers: turn erclient `_get` responses into a flat record list plus meta.

erclient's `_get` already strips the ER `{"data": ..., "status": ...}` envelope
and returns `data`. What is left is one of:

- a DRF page: `{"count": N, "next": <url|None>, "previous": ..., "results": [...]}`
- a plain list (e.g. /subjectgroups)
- a single object (a subject, /status, a GeoJSON FeatureCollection, ...)

`normalize_page` flattens any of those to `(records, next_url, count)`;
`follow_pages` walks `next` links; `fetch` does the first request and returns
the `{records, meta}` pieces every read command emits.
"""

from __future__ import annotations

from typing import Any

DEFAULT_PAGE_SIZE = 100


def normalize_page(data: Any) -> tuple[list, str | None, int | None]:
    """Return (records, next_url, count) for one already-unwrapped response."""
    if data is None:
        return [], None, 0
    if isinstance(data, dict) and "results" in data:
        return list(data.get("results") or []), data.get("next") or None, data.get("count")
    if isinstance(data, list):
        return list(data), None, len(data)
    return [data], None, 1


def follow_pages(client, page: Any, *, limit: int | None = None) -> tuple[list, int, int | None]:
    """Collect `page` and every page reachable through its `next` link.

    Stops as soon as `limit` records are in hand (no further requests) and
    trims any overshoot from the last page. `next` is the absolute URL ER
    returns; erclient's `_get` passes absolute URLs through untouched.
    Returns (records, pages_fetched, count_reported).
    """
    records, next_url, count = normalize_page(page)
    pages = 1
    while next_url and (limit is None or len(records) < limit):
        more, next_url, _ = normalize_page(client._get(next_url, max_retries=0))
        records.extend(more)
        pages += 1
    if limit is not None:
        records = records[:limit]
    return records, pages, count


def fetch(
    client,
    path: str,
    params: dict | None = None,
    *,
    paginate: bool = False,
    limit: int | None = None,
    version: str | None = None,
) -> tuple[list, dict]:
    """GET `path` (relative to the API root) and return (records, meta).

    `params` entries whose value is None are dropped. When `paginate` is
    true, `page_size` defaults to DEFAULT_PAGE_SIZE and `next` links are
    followed (see follow_pages). `version` selects a non-default API root
    (e.g. "v2.0" for subject tracks) via erclient's `_api_root`.
    """
    params = {k: v for k, v in (params or {}).items() if v is not None}
    if paginate and "page_size" not in params:
        params["page_size"] = DEFAULT_PAGE_SIZE
    base_url = client._api_root(version) if version else None
    page = client._get(path, base_url=base_url, params=params, max_retries=0)
    if paginate:
        records, pages, count = follow_pages(client, page, limit=limit)
    else:
        records, _, count = normalize_page(page)
        pages = 1
    meta: dict = {"total": len(records), "pages": pages}
    if count is not None:
        meta["count_reported"] = count
    return records, meta
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_read.py -q`
Expected: `12 passed`

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests
git add src/earthranger_cli/read.py tests/test_read.py
git commit -m "read: normalize DRF pages, follow next links, fetch(records, meta)"
```

---

### Task 2: Route the choices pagination through `read.follow_pages`

**Files:**
- Modify: `src/earthranger_cli/client.py:151-165`
- Test: existing `tests/test_client.py` (no changes expected)

**Interfaces:**
- Consumes: `read.follow_pages(client, page)` from Task 1.
- Produces: `client._collect_pages(client, page) -> list[dict]` keeps its signature; `get_choices` / `get_all_choices` unchanged for callers (`apply.py`, `pull.py`, `cli.py`).

- [ ] **Step 1: Run the existing choices tests to record the baseline**

Run: `uv run pytest tests/test_client.py -q`
Expected: all pass (this is the behavior we must preserve; note the count).

- [ ] **Step 2: Replace the body of `_collect_pages`**

Replace lines 151–165 of `src/earthranger_cli/client.py` (the whole `_collect_pages` function) with:

```python
def _collect_pages(client, page) -> list[dict]:
    """All records reachable from `page` (the first response) by following `next`."""
    from .read import follow_pages  # local import: read.py has no dependency on this module

    records, _pages, _count = follow_pages(client, page)
    return records
```

Nothing else in `client.py` changes. (`get_choices` keeps its own first `_get` with `max_retries=0` and its 400-means-empty handling.)

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -q`
Expected: same pass count as before plus Task 1's 12; specifically `test_get_choices_pages_through_results` still asserts the second call was `_get("https://x/choices?page=2", max_retries=0)` — `follow_pages` makes exactly that call.

- [ ] **Step 4: Lint and commit**

```bash
uv run ruff check src tests
git add src/earthranger_cli/client.py
git commit -m "client: _collect_pages delegates to read.follow_pages"
```

---

### Task 3: `output.py` — the `{records, meta}` writer

**Files:**
- Create: `src/earthranger_cli/output.py`
- Test: `tests/test_output.py`

**Interfaces:**
- Produces: `emit(records: list, meta: dict, output: str | None) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_output.py
import json

from earthranger_cli.output import emit


def test_emit_stdout_is_pretty_json_document(capsys):
    emit([{"id": "a"}], {"total": 1, "pages": 1}, None)
    out, err = capsys.readouterr()
    assert err == ""
    assert json.loads(out) == {"records": [{"id": "a"}], "meta": {"total": 1, "pages": 1}}
    assert out.startswith('{\n  "records"')  # indent=2
    assert out.endswith("\n")


def test_emit_file_writes_json_and_one_stderr_line(tmp_path, capsys):
    target = tmp_path / "nested" / "dir" / "out.json"
    emit([{"id": "a"}, {"id": "b"}], {"total": 2, "pages": 3}, str(target))
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"Done. 2 record(s) written to {target} (3 page(s)).\n"
    assert json.loads(target.read_text())["meta"]["pages"] == 3


def test_emit_serializes_non_json_values_with_str(capsys):
    from datetime import UTC, datetime

    emit([{"when": datetime(2026, 9, 23, tzinfo=UTC)}], {"total": 1, "pages": 1}, None)
    out, _ = capsys.readouterr()
    assert json.loads(out)["records"][0]["when"] == "2026-09-23 00:00:00+00:00"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_output.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'earthranger_cli.output'`

- [ ] **Step 3: Write the implementation**

```python
# src/earthranger_cli/output.py
"""Uniform read output: {"records": [...], "meta": {...}} to stdout or a file.

This is er-cli's (and the Skylight CLI's) shape, so agent skills can
"write to -o PATH, then read the file" identically across tools.
"""

from __future__ import annotations

import json
from pathlib import Path

import click


def emit(records: list, meta: dict, output: str | None) -> None:
    doc = {"records": records, "meta": meta}
    text = json.dumps(doc, indent=2, default=str)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n")
        click.echo(
            f"Done. {meta.get('total', len(records))} record(s) written to {output} "
            f"({meta.get('pages', 1)} page(s)).",
            err=True,
        )
    else:
        click.echo(text)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_output.py -q`
Expected: `3 passed`

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests
git add src/earthranger_cli/output.py tests/test_output.py
git commit -m "output: emit {records, meta} JSON to stdout or -o PATH"
```

---

### Task 4: `read_commands.py` — declarative table compiled into click commands, wired into `main`

**Files:**
- Create: `src/earthranger_cli/read_commands.py`
- Modify: `tests/conftest.py` (extend `FakeER`)
- Modify: `src/earthranger_cli/cli.py` (append registration at the very end of the file)
- Test: `tests/test_read_commands.py`

**Interfaces:**
- Consumes: `read.fetch`, `output.emit`; from `cli.py` via injection: `_connect(ctx)`, `connection_options`, `_api_errors`.
- Produces:
  - `Flag(param, help, kind="str")`, `ReadCommand(group, name, path, help, kind="list", flags=(), version=None, arg=None)`, `Deps(connect, connection_options, api_errors)`.
  - `COMMANDS: tuple[ReadCommand, ...]`, `GROUP_HELP: dict[str, str]`.
  - `register(main: click.Group, deps: Deps) -> None` — adds each command to `main.commands[group]`, creating the group if absent.
  - CLI: `er status show`, `er auth whoami`, `er subjects search|get`, `er tracks get`, `er observations search`, `er events search|get`, `er patrols search|get`, `er sources search|get`, `er subject-groups list|get`, `er subject-sources search`, `er fences list`, `er featuresets get`, `er regions list`; every one takes `-o/--output PATH`; every `list` kind takes `--limit N`.

- [ ] **Step 1: Extend `FakeER` so tests can script arbitrary GET responses**

In `tests/conftest.py`, inside `FakeER.__init__`, add after `self.me = ...`:

```python
        # path (or absolute next-URL) -> canned response, or a list of responses
        # consumed in order (so a path requested twice can page)
        self.responses: dict = {}
```

Add this method to `FakeER` (anywhere after `get_me`):

```python
    def _api_root(self, version):
        return f"https://fake.pamdas.org/api/{version}"
```

Replace the existing `_get` method with:

```python
    def _get(self, path, base_url=None, params=None, max_retries=5, **kwargs):
        if path == "user/me":
            self.calls.append(("get_me", max_retries))
            return self.me
        if path in self.responses:
            self.calls.append(("_get", path, params, base_url, max_retries))
            canned = self.responses[path]
            if isinstance(canned, list):
                return canned.pop(0)
            return canned
        self.calls.append(("_get", path, params))
        field = (params or {}).get("field")
        if field is None:
            results = [r for recs in self.choices.values() for r in recs]
        else:
            results = list(self.choices.get(field, []))
        return {"results": results, "next": None}
```

Run: `uv run pytest -q` — Expected: still all green (choices tests append the 3-tuple exactly as before).

- [ ] **Step 2: Write the failing tests**

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_read_commands.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'earthranger_cli.read_commands'`

- [ ] **Step 4: Write `read_commands.py`**

```python
# src/earthranger_cli/read_commands.py
"""Read-only `er <resource> <action>` commands, declared as data.

Each ReadCommand row names an ER GET endpoint and the query params it takes;
`register` compiles the rows into click commands and attaches them to the
main group. Flags are hand-written here (a curated subset of the OpenAPI
spec); switching to spec-derived flags is a P1 item in the parity spec.

Output is always er-cli's {"records": [...], "meta": {...}} JSON (see
output.emit) so agent skills can consume it unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import click

from .output import emit
from .read import fetch


@dataclass(frozen=True)
class Flag:
    param: str  # ER query parameter name, e.g. "updated_since"
    help: str
    kind: str = "str"  # "str" | "int" | "float" | "bool"


@dataclass(frozen=True)
class ReadCommand:
    group: str  # top-level group, e.g. "subjects" (created if missing)
    name: str  # action, e.g. "search"
    path: str  # relative to the API root; "{id}" is replaced by the positional
    help: str
    kind: str = "list"  # "list" (paginated, has --limit) | "get" (single object)
    flags: tuple[Flag, ...] = ()
    version: str | None = None  # e.g. "v2.0"; None = erclient default (v1.0)
    arg: str | None = None  # positional name shown in help, e.g. "subject_id"


@dataclass(frozen=True)
class Deps:
    """What the commands need from cli.py, injected so this module never imports it
    (and so tests that monkeypatch cli._connect keep working)."""

    connect: Callable[[click.Context], object]
    connection_options: Callable
    api_errors: Callable


GROUP_HELP: dict[str, str] = {
    "status": "Server status.",
    "subjects": "Read subjects (animals / collars).",
    "tracks": "Read subject tracks (GeoJSON).",
    "observations": "Read raw GPS observations.",
    "patrols": "Read patrols.",
    "sources": "Read collar / device sources.",
    "subject-groups": "Read subject groups.",
    "subject-sources": "Read subject-to-source assignments.",
    "fences": "Read spatial feature groups (geofences).",
    "featuresets": "Read featuresets (GeoJSON boundaries).",
    "regions": "Read operational regions.",
}

_PAGE_SIZE = Flag("page_size", "Records per page (default 100).", "int")
_INCLUDE_INACTIVE = Flag("include_inactive", "Include inactive records.", "bool")

COMMANDS: tuple[ReadCommand, ...] = (
    ReadCommand("status", "show", "status", "Server status and current server time.", "get"),
    ReadCommand("auth", "whoami", "user/me", "Show the authenticated user (/user/me/).", "get"),
    ReadCommand(
        "subjects",
        "search",
        "subjects",
        "Search subjects.",
        flags=(
            Flag("name", "Filter by subject name."),
            Flag("subject_group", "Subject group id."),
            Flag("subject_subtypes", "Comma-separated subject subtypes."),
            _INCLUDE_INACTIVE,
            Flag("updated_since", "ISO-8601; subjects updated after this time."),
            Flag("position_updated_since", "ISO-8601; subjects with a position after this time."),
            Flag("render_last_location", "Include each subject's last location.", "bool"),
            Flag("tracks", "Include recent tracks.", "bool"),
            Flag("tracks_since", "ISO-8601 start for --tracks."),
            Flag("tracks_until", "ISO-8601 end for --tracks."),
            Flag("bbox", "Bounding box: west,south,east,north."),
            _PAGE_SIZE,
        ),
    ),
    ReadCommand("subjects", "get", "subject/{id}", "Retrieve one subject.", "get", arg="subject_id"),
    ReadCommand(
        "tracks",
        "get",
        "subject/{id}/tracks",
        "Retrieve a subject's track (GeoJSON) for a time window.",
        "get",
        flags=(
            Flag("since", "ISO-8601 start."),
            Flag("until", "ISO-8601 end."),
            Flag("show_excluded", "Include points ER excluded as outliers.", "bool"),
            Flag("max_speed_kmh", "Drop segments faster than this.", "float"),
            Flag("max_gap_minutes", "Break the track at gaps longer than this.", "float"),
        ),
        version="v2.0",
        arg="subject_id",
    ),
    ReadCommand(
        "observations",
        "search",
        "observations",
        "Search raw GPS observations.",
        flags=(
            Flag("subject_id", "Subject id."),
            Flag("source_id", "Source id."),
            Flag("subjectsource_id", "Subject-source assignment id."),
            Flag("since", "ISO-8601 start."),
            Flag("until", "ISO-8601 end."),
            Flag("filter", "ER observation filter (e.g. 0 = exclusion flags off)."),
            Flag("include_details", "Include observation details.", "bool"),
            Flag("bbox", "Bounding box: west,south,east,north."),
            Flag("sort_by", "Sort field (prefix '-' for descending)."),
            _PAGE_SIZE,
        ),
    ),
    ReadCommand(
        "events",
        "search",
        "activity/events",
        "Search activity events.",
        flags=(
            Flag("event_type", "Event type id(s), comma-separated (ids, not values — see P1)."),
            Flag("event_category", "Event category value."),
            Flag("state", "new | active | resolved (comma-separated allowed)."),
            Flag("updated_since", "ISO-8601; events updated after this time."),
            Flag("filter", "ER events JSON filter."),
            Flag("bbox", "Bounding box: west,south,east,north."),
            Flag("sort_by", "Sort field (prefix '-' for descending)."),
            Flag("include_details", "Include event_details.", "bool"),
            Flag("include_notes", "Include notes.", "bool"),
            Flag("include_updates", "Include the update history.", "bool"),
            Flag("include_files", "Include attached files.", "bool"),
            Flag("include_related_events", "Include related events.", "bool"),
            Flag("is_collection", "Only incident collections.", "bool"),
            Flag("exclude_contained", "Exclude events contained in a collection.", "bool"),
            _PAGE_SIZE,
        ),
    ),
    ReadCommand("events", "get", "activity/event/{id}", "Retrieve one event.", "get", arg="event_id"),
    ReadCommand(
        "patrols",
        "search",
        "activity/patrols",
        "Search patrols.",
        flags=(
            Flag("filter", "ER patrols JSON filter."),
            Flag("state", "open | done | cancelled."),
            Flag("exclude_empty_patrols", "Skip patrols with no segments.", "bool"),
            _PAGE_SIZE,
        ),
    ),
    ReadCommand("patrols", "get", "activity/patrols/{id}", "Retrieve one patrol.", "get", arg="patrol_id"),
    ReadCommand("sources", "search", "sources", "Search collar / device sources.", flags=(_PAGE_SIZE,)),
    ReadCommand("sources", "get", "source/{id}", "Retrieve one source.", "get", arg="source_id"),
    ReadCommand(
        "subject-groups",
        "list",
        "subjectgroups",
        "List subject groups.",
        flags=(
            _INCLUDE_INACTIVE,
            Flag("include_hidden", "Include groups that are not visible.", "bool"),
            Flag("flat", "Flatten nested groups into one list.", "bool"),
            Flag("group_name", "Filter by group name."),
        ),
    ),
    ReadCommand(
        "subject-groups", "get", "subjectgroup/{id}", "Retrieve one subject group.", "get", arg="group_id"
    ),
    ReadCommand(
        "subject-sources",
        "search",
        "subjectsources",
        "Search subject-to-source assignments (which collar tracked which subject when).",
        flags=(
            Flag("subjects", "Comma-separated subject ids."),
            Flag("sources", "Comma-separated source ids."),
            _PAGE_SIZE,
        ),
    ),
    ReadCommand(
        "fences",
        "list",
        "spatialfeaturegroup",
        "List spatial feature groups (geofences).",
        flags=(Flag("sort_by", "Sort field."),),
    ),
    ReadCommand(
        "featuresets", "get", "featureset/{id}", "Retrieve a featureset (GeoJSON boundaries).", "get",
        arg="featureset_id",
    ),
    ReadCommand("regions", "list", "regions", "List operational regions."),
)

_TYPES = {"str": str, "int": int, "float": float}


def _option_names(param: str) -> list[str]:
    """`--updated-since` plus er-cli's `--updated_since`; the last entry is the
    Python destination name click passes to the callback."""
    names = ["--" + param.replace("_", "-")]
    if "_" in param:
        names.append("--" + param)
    names.append(param)
    return names


def _endpoint(spec: ReadCommand) -> str:
    path = spec.path.replace("{id}", "{" + (spec.arg or "id") + "}")
    return f"[GET /api/{spec.version or 'v1.0'}/{path}]"


def _make_command(spec: ReadCommand, deps: Deps) -> click.Command:
    def callback(ctx, output, limit=None, **kwargs):
        client = deps.connect(ctx)
        path = spec.path
        if spec.arg:
            path = path.replace("{id}", kwargs.pop(spec.arg))
        params: dict = {}
        for flag in spec.flags:
            value = kwargs.get(flag.param)
            if flag.kind == "bool":
                if value:
                    params[flag.param] = "true"
            elif value is not None:
                params[flag.param] = value
        records, meta = fetch(
            client, path, params, paginate=spec.kind == "list", limit=limit, version=spec.version
        )
        emit(records, meta, output)

    callback.__name__ = f"{spec.group}_{spec.name}".replace("-", "_")
    fn = deps.api_errors(callback)
    fn = click.pass_context(fn)
    for flag in reversed(spec.flags):
        if flag.kind == "bool":
            fn = click.option(*_option_names(flag.param), is_flag=True, help=flag.help)(fn)
        else:
            fn = click.option(*_option_names(flag.param), type=_TYPES[flag.kind], help=flag.help)(fn)
    if spec.kind == "list":
        fn = click.option("--limit", type=int, metavar="N", help="Cap total records returned.")(fn)
    fn = click.option(
        "-o", "--output", type=click.Path(dir_okay=False), help="Write JSON here instead of stdout."
    )(fn)
    if spec.arg:
        fn = click.argument(spec.arg)(fn)
    fn = deps.connection_options(fn)
    return click.command(spec.name, help=f"{spec.help}\n\n{_endpoint(spec)}")(fn)


def register(main: click.Group, deps: Deps) -> None:
    """Attach every COMMANDS row to `main`, creating resource groups as needed.
    Rows whose group already exists (events, auth) are added to that group."""
    for spec in COMMANDS:
        group = main.commands.get(spec.group)
        if group is None:
            group = click.Group(spec.group, help=GROUP_HELP[spec.group])
            main.add_command(group)
        group.add_command(_make_command(spec, deps))
```

- [ ] **Step 5: Register from `cli.py`**

Append to the very end of `src/earthranger_cli/cli.py` (after the last command, `choices_show`):

```python


# --- read-only resource commands (er-cli parity) ---------------------------
# Registered last so every group they extend (events, auth) already exists.
# `_connect` is looked up at call time so tests can monkeypatch it.
from . import read_commands as _read_commands  # noqa: E402

_read_commands.register(
    main,
    _read_commands.Deps(
        connect=lambda ctx: _connect(ctx),
        connection_options=connection_options,
        api_errors=_api_errors,
    ),
)
```

- [ ] **Step 6: Run the new tests and the full suite**

Run: `uv run pytest tests/test_read_commands.py -q`
Expected: `12 passed`

Run: `uv run pytest -q`
Expected: all green. If `test_api_error_is_reported_cleanly` fails on the message, check `client.describe_error`: `ERClientNotFound()` stringifies to `None`, which `describe_error` turns into `NotFound (no details from the server)` — the assertion `startswith("error: NotFound")` is on that path.

- [ ] **Step 7: Try it for real against a site you have a profile for (manual smoke)**

```bash
uv run er status show
uv run er auth whoami
uv run er subjects search --limit 3 -o /tmp/subj.json && head -c 300 /tmp/subj.json
uv run er tracks get --help
```

Expected: JSON documents with `records`/`meta`; the `-o` run prints one `Done. ...` line on stderr and nothing on stdout.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check src tests
git add src/earthranger_cli/read_commands.py src/earthranger_cli/cli.py tests/conftest.py tests/test_read_commands.py
git commit -m "Add read-only resource commands with {records, meta} JSON output and auto-pagination"
```

---

### Task 5: `--json` / `-o` on the three existing read commands

**Files:**
- Modify: `src/earthranger_cli/cli.py:396-449` (`list_categories`, `list_event_types`, `show_event_type`)
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `output.emit` (Task 3).
- Produces: `er events list categories [--json] [-o PATH]`, `er events list event-types [--category V] [--json] [-o PATH]`, `er events show event-type VALUE [--json] [-o PATH]`. Human output is unchanged when neither flag is given. `-o` implies `--json`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py` (the file already has `json`, `fake`, and `_run` in scope):

```python
# --- --json / -o on the existing read commands ---


def test_list_categories_json_contract(fake):
    fake.categories = [{"value": "wm", "display": "Wildlife Monitoring", "is_active": True}]
    result = _run(["events", "list", "categories", "--json"])
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert doc["records"] == fake.categories
    assert doc["meta"] == {"total": 1, "pages": 1}


def test_list_event_types_json_applies_category_filter(fake):
    fake.event_types = [
        {"value": "a", "display": "A", "category": {"value": "wm"}},
        {"value": "b", "display": "B", "category": "other"},
    ]
    result = _run(["events", "list", "event-types", "--category", "wm", "--json"])
    doc = json.loads(result.output)
    assert [r["value"] for r in doc["records"]] == ["a"]
    assert doc["meta"] == {"total": 1, "pages": 1}


def test_show_event_type_json_wraps_the_existing_document(fake):
    fake.event_types = [{"value": "s", "display": "S", "category": "wm", "schema": {}}]
    result = _run(["events", "show", "event-type", "s", "--json"])
    doc = json.loads(result.output)
    assert doc["records"] == [{"event_type": fake.event_types[0], "choices": {}}]
    assert doc["meta"] == {"total": 1, "pages": 1}


def test_output_flag_implies_json_and_writes_file(fake, tmp_path):
    fake.categories = [{"value": "wm", "display": "Wildlife Monitoring"}]
    target = tmp_path / "cats.json"
    runner = CliRunner()
    result = runner.invoke(
        main, ["events", "list", "categories", "-o", str(target)], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "1 record(s) written to" in result.stderr
    assert json.loads(target.read_text())["records"][0]["value"] == "wm"


def test_human_output_unchanged_without_json_flag(fake):
    fake.categories = [{"value": "wm", "display": "Wildlife Monitoring", "is_active": True}]
    result = _run(["events", "list", "categories"])
    assert result.output.startswith("wm")
    assert "records" not in result.output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -k "json or output_flag or human_output" -q`
Expected: FAIL with `Error: No such option: --json` (exit code 2) for the first four; the last one passes already.

- [ ] **Step 3: Add the shared option decorator and update the three commands**

In `src/earthranger_cli/cli.py`, add near the top (after the `from .pull import ...` line):

```python
from .output import emit
```

Add this helper just above `@events_group.group("list")` (line ~391):

```python
def json_output_options(f):
    """Opt-in agent output for the older read commands: --json switches to the
    {records, meta} contract; -o implies --json and writes the document to a file."""
    f = click.option(
        "-o", "--output", type=click.Path(dir_okay=False), help="Write JSON here (implies --json)."
    )(f)
    f = click.option("--json", "json_", is_flag=True, help="Emit {records, meta} JSON.")(f)
    return f
```

Replace `list_categories` with:

```python
@list_group.command("categories")
@connection_options
@json_output_options
@click.pass_context
@_api_errors
def list_categories(ctx, json_, output):
    """List event categories (inactive included)."""
    client = _connect(ctx)
    categories = list(client.get_event_categories(include_inactive=True))
    if json_ or output:
        emit(categories, {"total": len(categories), "pages": 1}, output)
        return
    for c in categories:
        active = "" if c.get("is_active", True) else "  (inactive)"
        click.echo(f"{c.get('value'):<40} {c.get('display')}{active}")
```

Replace `list_event_types` with:

```python
@list_group.command("event-types")
@connection_options
@click.option("--category", help="Filter by category value.")
@json_output_options
@click.pass_context
@_api_errors
def list_event_types(ctx, category, json_, output):
    """List event types (inactive included)."""
    client = _connect(ctx)
    types = [
        t
        for t in client.get_event_types(include_inactive=True, version="v2.0")
        if not category or _category_value_of(t) == category
    ]
    if json_ or output:
        emit(types, {"total": len(types), "pages": 1}, output)
        return
    for t in types:
        active = "" if t.get("is_active", True) else "  (inactive)"
        click.echo(
            f"{t.get('value'):<40} {t.get('display'):<40} {_category_value_of(t)}{active}"
        )
```

Replace `show_event_type` with:

```python
@show_group.command("event-type")
@connection_options
@click.argument("value")
@json_output_options
@click.pass_context
@_api_errors
def show_event_type(ctx, value, json_, output):
    """Print the full v2 event type JSON plus its referenced Choice records."""
    client = _connect(ctx)
    types = client.get_event_types(include_inactive=True, include_schema=True, version="v2.0")
    et = next((t for t in types if t.get("value") == value), None)
    if et is None:
        click.echo(f"error: no event type with value {value!r}")
        sys.exit(1)
    fields = extract_choice_fields(normalize_v2_schema(et.get("schema") or {}))
    choices = {f: er.get_choices(client, f) for f in fields}
    doc = {"event_type": et, "choices": choices}
    if json_ or output:
        emit([doc], {"total": 1, "pages": 1}, output)
        return
    click.echo(json.dumps(doc, indent=2))
```

- [ ] **Step 4: Run the tests and the full suite**

Run: `uv run pytest tests/test_cli.py -q`
Expected: all pass, including the pre-existing `test_list_categories`, `test_list_event_types_filters_by_category`, `test_show_event_type_*` (human output byte-identical).

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src tests
git add src/earthranger_cli/cli.py tests/test_cli.py
git commit -m "events list/show: opt-in --json and -o using the {records, meta} contract"
```

---

### Task 6: Docs, spec checkboxes, version bump

**Files:**
- Modify: `README.md` (Commands table at lines ~213–225; new section after it)
- Modify: `docs/superpowers/specs/2026-09-02-er-cli-parity-design.md:167-174` (P0 checkboxes)
- Modify: `src/earthranger_cli/__init__.py:3`
- Test: `tests/test_package.py` (unchanged; asserts `__version__` is truthy)

- [ ] **Step 1: README — Commands table**

In the `## Commands` table, after the `events pull` row and before the `auth login` row, insert:

```markdown
| `events search [--event-type ID ...] [--limit N] [-o F]` | Search events; JSON `{records, meta}` output |
| `events get EVENT_ID [-o F]` | One event as a one-record `{records, meta}` document |
| `events list categories\|event-types [--json] [-o F]`, `events show event-type V [--json] [-o F]` | Same as above, opt-in `{records, meta}` output |
| `status show`, `auth whoami` | Server status; the authenticated user |
| `subjects search\|get`, `tracks get SUBJECT_ID`, `observations search` | Read subjects, tracks (v2 GeoJSON), raw observations |
| `patrols search\|get`, `sources search\|get`, `subject-groups list\|get`, `subject-sources search` | Read patrols, sources, groups, collar↔subject assignments |
| `fences list`, `featuresets get ID`, `regions list` | Read geofence groups, GeoJSON boundaries, operational regions |
```

- [ ] **Step 2: README — new section**

Insert this section immediately after the Commands table (before `## Spec reference`):

````markdown
## Reading data (agent-friendly JSON)

Every read command above emits one JSON document in the same shape as
tusker's `er-cli` and the Skylight CLI, so agent skills can "write to
`-o PATH`, then read the file" identically across tools:

```json
{"records": [...], "meta": {"total": 12, "pages": 1, "count_reported": 12}}
```

- `records` is always a flat list — one element for `get` commands and for
  GeoJSON FeatureCollections.
- List commands auto-paginate (following ER's `next` link, 100 per page by
  default; `--page-size` overrides). `--limit N` caps the total and stops
  requesting pages once it is reached. `meta.pages` reports how many pages
  were fetched; `meta.count_reported` is ER's own total when it sends one.
- `-o PATH` writes the document (creating parent directories) and prints a
  single `Done. N record(s) written to PATH (P page(s)).` line to stderr;
  stdout stays empty. Without `-o`, the document is pretty-printed to stdout.
- Flags are the ER query parameters, spelled either `--updated-since` or
  er-cli style `--updated_since`. Run `er <resource> <action> --help` to see
  the endpoint each command calls, e.g. `[GET /api/v1.0/subjects]`.
- The pre-existing `events list ...` and `events show event-type` keep their
  human output by default; pass `--json` (or `-o`) for the contract above.

Typical skill one-liner:

```bash
er events search --event-type <uuid> --updated-since 2026-09-01T00:00:00Z -o /tmp/ev.json
```

`--event-type` takes event-type **ids** for now; resolving friendly values
like `geofence_break` to ids is on the backlog (see
`docs/superpowers/specs/2026-09-02-er-cli-parity-design.md`).
````

- [ ] **Step 3: Tick the P0 boxes in the spec**

In `docs/superpowers/specs/2026-09-02-er-cli-parity-design.md`, change the P0 list to:

```markdown
- [x] Uniform JSON output (`--json`, `-o/--output`) on every read command,
      er-cli's exact `{records, meta}` shape. (2026-09-23,
      `docs/superpowers/plans/2026-09-23-read-surface.md`)
- [x] Bearer-token auth path (`--token`, `ER_TOKEN`, `auth login --token`);
      precedence decided — see §Bearer-token auth.
- [x] Read-only resource commands per the table above — hand-written flags;
      spec-derived flags remain the P1 decision. (2026-09-23)
- [x] Shared pagination helper with `--limit` and envelope unwrapping
      (`read.fetch` / `read.follow_pages`; choices use it too). (2026-09-23)
```

Also change the header line `Status: Draft — captured from a comparison session; not yet approved for implementation. Revisit before starting any P0 item.` to `Status: P0 implemented 2026-09-23; P1/P2 open.`

- [ ] **Step 4: Bump the version**

In `src/earthranger_cli/__init__.py` change `__version__ = "0.1.0"` to `__version__ = "0.2.0"`.
(This is a feature release; the tag `v0.2.0` is pushed separately once the PR merges — pushing the tag publishes to PyPI.)

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest -q
uv run ruff check src tests
uv build && ls dist/ | grep 0.2.0
git add README.md docs/superpowers/specs/2026-09-02-er-cli-parity-design.md src/earthranger_cli/__init__.py
git commit -m "Document the read surface; tick P0 parity items; bump to 0.2.0"
```

---

## Self-review notes

- **Spec coverage.** §Output contract → Tasks 3, 4, 5 (shape, `-o` semantics, stderr line, human default kept). §Read-only resource commands table → Task 4 `COMMANDS` covers every row: status, whoami, subjects, tracks (v2 via `_api_root`), observations, events search/get, patrols, sources, subject-groups, subject-sources, fences, featuresets, regions. §Pagination → Tasks 1–2 (`follow_pages` shared with choices, `--limit`, `page_size=100`, `pages` in meta). §Naming → events nested, everything else flat; underscored aliases give er-cli compatibility. Retries, name→id resolution, `--version`, flat `event-types list` alias are explicitly P1 and deliberately absent.
- **Not in scope (from the spec's own table but no erclient-shaped need):** nothing omitted from the resource table.
- **Type consistency.** `fetch` returns `(list, dict)`; `follow_pages` returns `(list, int, int | None)`; `emit(records, meta, output)` is called with exactly those in Tasks 4 and 5. `FakeER._get` records `("_get", path, params, base_url, max_retries)` for scripted paths and the tests index `[1]`, `[2]`, `[3]`, `[4]` accordingly; unscripted (choices) paths keep the old 3-tuple.
- **Known risk to flag in the PR.** Following the absolute `next` URL assumes ER returns a publicly reachable host in `next`. erclient's own `get_events` rewrites `next` to a relative path for some proxied deployments. The choices helper has followed absolute `next` in production without issue, so this plan keeps that behavior; if a site returns internal hostnames, the fix is a one-line change in `follow_pages` to re-request `path` with the `page` query param parsed from `next`.
