# P3 — otus parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the read surface the agent-facing features the otus er-cli grew from evaluation runs — column projection, site-timezone windows, counts and grouping, server CSV exports, a detail filter, a page cap, pattern name matching — without touching the DSL, `apply`, `pull`, or any human output.

**Architecture:** Everything hangs off the data-declared read commands in `read_commands.py`. Three new small modules carry the logic so that file stays a wiring layer: `clock.py` (site timezone, `/status` clock, day/last bounds, period buckets), `windows.py` (`--since/--until/--today/--yesterday/--last` parsing and folding into each endpoint's spelling), `aggregate.py` (`--count-only`, `--group-by`, `--where`). `output.py` grows `--fields`/`--format`; `read.py` grows the page cap, a raw-text fetch and a count fetch. The authoring commands in `cli.py` gain only `--fields`/`--format`, active under `--json`/`-o`.

**Tech Stack:** Python 3.11, click, erclient (requests), pytest with the `FakeER` fixture in `tests/conftest.py`. Run everything with `uv run --extra dev ...`.

**Spec:** `docs/superpowers/specs/2026-09-02-er-cli-parity-design.md` — §Comparison with otus er-cli (as of 2026-10-09) and §P3 — otus parity, including its guardrails.

## Global Constraints

- Python `>=3.11`; no new runtime dependencies (`zoneinfo`, `fnmatch`, `difflib`, `csv` are stdlib).
- `uv run --extra dev pytest -q` green and `uv run --extra dev ruff check src tests && uv run --extra dev ruff format --check src tests` clean at every commit.
- Spec guardrails: the DSL, `apply`, `pull`, and the default human output of `events list` / `events show` are unchanged byte for byte. `--fields`/`--format` on those commands act only under `--json` or `-o`. Glob matching applies to `events search --event-type` and `subjects search --subject-group` only; `events post --event-type`, `apply` and `pull` never resolve patterns.
- Output contract (spec §Output contract): the `Done.` line stays on **stderr**; stdout carries only the document; large output is never spilled to a temp file; errors are never mirrored to both streams.
- Flag spelling: every new flag answers to `--kebab-case` and er-cli's `--snake_case` (via `_option_names`), so otus skills port unchanged: `--count_only`, `--group_by`, `--page_size`, `--value_cols`.
- Exit codes: 2 for usage errors raised before or after connecting (`click.UsageError`), 1 for API failures and for "the site reported no usable timezone" (`click.ClickException`).
- Commit messages: plain descriptive, body explains why, trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. **A bare `--until 2026-08-31`** must mean the end of that day, not midnight opening it; otherwise "August" silently drops the 31st. Pinned in Task 5.
2. **`--last 24h` across a DST change** must be 24 real hours: subtract in UTC, then convert to site time. Pinned in Task 3 with `America/Los_Angeles` on 2026-11-01.
3. **A site that reports only a zone abbreviation** (`server_timezone: "EAT"`, no IANA name) has no usable timezone: `--today` must refuse with exit 1, never answer in UTC; passive meta must omit `site_now`/`site_tz` rather than crash. Pinned in Tasks 3 and 4.
4. **`--fields` into a list with an out-of-range index** (`coordinates.5`) or a name where a list needs an index must give an empty cell, never a traceback. Pinned in Task 1.
5. **An export 403 with a flag the records endpoint cannot express** (`observations export --current-status`) must surface the 403, not silently widen the question into a whole-site records pull. Pinned in Task 7.

---

### Task 1: `--fields` projection and `--format tsv|csv` in `output.py`

**Files:**
- Modify: `src/earthranger_cli/output.py`
- Modify: `src/earthranger_cli/read_commands.py:434-489` (`_make_command`: add the two options)
- Modify: `src/earthranger_cli/cli.py:460-467` (`json_output_options`) and the three callers at `:470-540`
- Test: `tests/test_output.py`, `tests/test_read_commands.py`, `tests/test_cli.py`

**Interfaces:**
- Produces (used by Tasks 4, 6, 7, 8):
  ```python
  FORMATS = ("json", "tsv", "csv")
  def parse_fields(raw: str | None) -> list[str] | None
  def pluck(record, path: str)                      # dotted path, list index, None when missing
  def project(records: list, fields: list[str] | None) -> list
  def cell(value) -> str                             # "" for None, compact JSON for dict/list, "true"/"false"
  def render(records, meta, fields, fmt) -> str
  def emit(records, meta, output, *, fields=None, fmt="json") -> None
  def output_options(f)                              # click decorator: --fields, --format
  def check_format(fields, fmt) -> None              # UsageError when tsv/csv without --fields
  ```

- [ ] **Step 1: Write the failing tests for pluck/project/render**

Append to `tests/test_output.py`:

```python
import pytest
import click

from earthranger_cli.output import cell, check_format, parse_fields, pluck, project, render


def test_parse_fields_splits_and_strips():
    assert parse_fields("id, name ,reported_by.name") == ["id", "name", "reported_by.name"]
    assert parse_fields(None) is None
    assert parse_fields("") is None


def test_pluck_follows_dotted_paths_and_list_indexes():
    rec = {"id": "a", "last_position": {"geometry": {"coordinates": [36.8, -1.3]}}, "tags": ["x"]}
    assert pluck(rec, "id") == "a"
    assert pluck(rec, "last_position.geometry.coordinates.1") == -1.3
    assert pluck(rec, "last_position.geometry.coordinates.-1") == -1.3
    assert pluck(rec, "missing.deep") is None
    # Review Focus 4: out-of-range index and a name where a list needs an index
    assert pluck(rec, "last_position.geometry.coordinates.5") is None
    assert pluck(rec, "tags.name") is None
    assert pluck(rec, "id.anything") is None


def test_project_keeps_only_requested_fields():
    recs = [{"id": "a", "n": 1, "x": {"y": 2}}, {"id": "b"}]
    assert project(recs, ["id", "x.y"]) == [{"id": "a", "x.y": 2}, {"id": "b", "x.y": None}]
    assert project(recs, None) is recs


def test_cell_renders_nested_values_as_compact_json():
    assert cell(None) == ""
    assert cell(True) == "true"
    assert cell({"a": [1, 2]}) == '{"a":[1,2]}'
    assert cell(3) == "3"


def test_render_tsv_escapes_tabs_and_newlines():
    recs = [{"id": "a", "title": "tab\there\nnew"}]
    out = render(recs, {}, ["id", "title"], "tsv")
    assert out == "id\ttitle\na\ttab\\there\\nnew\n"


def test_render_csv_quotes_commas():
    recs = [{"id": "a", "title": "x, y"}]
    assert render(recs, {}, ["id", "title"], "csv") == 'id,title\na,"x, y"\n'


def test_render_json_projects_records_and_keeps_meta():
    out = json.loads(render([{"id": "a", "n": 1}], {"total": 1, "pages": 1}, ["id"], "json"))
    assert out == {"records": [{"id": "a"}], "meta": {"total": 1, "pages": 1}}


def test_check_format_requires_fields_for_tables():
    check_format(None, "json")
    check_format(["id"], "tsv")
    with pytest.raises(click.UsageError, match="--format tsv needs --fields"):
        check_format(None, "tsv")


def test_emit_tsv_to_file_keeps_done_line_on_stderr(tmp_path, capsys):
    target = tmp_path / "t.tsv"
    emit([{"id": "a"}], {"total": 1, "pages": 1}, str(target), fields=["id"], fmt="tsv")
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"Done. 1 record(s) written to {target} (1 page(s)).\n"
    assert target.read_text() == "id\na\n"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --extra dev pytest tests/test_output.py -q`
Expected: ImportError on `cell` / `parse_fields` etc.

- [ ] **Step 3: Implement in `output.py`**

Replace the file body after the module docstring with:

```python
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import click

FORMATS = ("json", "tsv", "csv")
_TSV_ESCAPES = str.maketrans({"\t": "\\t", "\n": "\\n", "\r": "\\r"})


def parse_fields(raw: str | None) -> list[str] | None:
    """`"id,name,reported_by.name"` -> the paths, or None when not asked for."""
    if not raw:
        return None
    return [f.strip() for f in raw.split(",") if f.strip()] or None


def pluck(record, path: str):
    """Follow a dotted path into a record; a numeric segment indexes a list.

    Missing is not an error: the point is extracting a column across records
    that do not all carry it, so a path that is not there is None (an empty
    cell), including an index past either end of a list.
    """
    value = record
    for part in path.split("."):
        if isinstance(value, list):
            try:
                index = int(part)
            except ValueError:
                return None
            if not -len(value) <= index < len(value):
                return None
            value = value[index]
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
        if value is None:
            return None
    return value


def project(records: list, fields: list[str] | None) -> list:
    if not fields:
        return records
    return [{f: pluck(r, f) for f in fields} for r in records]


def cell(value) -> str:
    """One value as text: None is empty, dicts and lists are compact JSON (not
    Python repr), booleans are lower-case so `grep true` works."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), default=str)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _tsv(records: list, fields: list[str]) -> str:
    # escape rather than quote: `cut -f2` and `awk -F'\t'` do not unquote
    rows = [fields] + [[cell(pluck(r, f)) for f in fields] for r in records]
    return "".join("\t".join(c.translate(_TSV_ESCAPES) for c in row) + "\n" for row in rows)


def _csv(records: list, fields: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(fields)
    for record in records:
        writer.writerow([cell(pluck(record, f)) for f in fields])
    return buf.getvalue()


def render(records: list, meta: dict, fields: list[str] | None, fmt: str) -> str:
    if fmt == "json":
        doc = {"records": project(records, fields), "meta": meta}
        return json.dumps(doc, indent=2, default=str) + "\n"
    return (_tsv if fmt == "tsv" else _csv)(records, fields or [])


def check_format(fields: list[str] | None, fmt: str) -> None:
    if fmt != "json" and not fields:
        raise click.UsageError(f"--format {fmt} needs --fields to say which columns to write.")


def emit(
    records: list,
    meta: dict,
    output: str | None,
    *,
    fields: list[str] | None = None,
    fmt: str = "json",
) -> None:
    text = render(records, meta, fields, fmt)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        click.echo(
            f"Done. {meta.get('total', len(records))} record(s) written to {output} "
            f"({meta.get('pages', 1)} page(s)).",
            err=True,
        )
    else:
        click.echo(text, nl=False)


def output_options(f):
    """`--fields` and `--format` for any command that emits the records document."""
    f = click.option(
        "--format",
        "fmt",
        type=click.Choice(FORMATS),
        default="json",
        show_default=True,
        help="json is the {records, meta} document; tsv/csv write a table of --fields.",
    )(f)
    f = click.option(
        "--fields",
        help="Comma-separated dotted paths to keep, e.g. id,name,last_position.geometry.coordinates.1.",
    )(f)
    return f
```

- [ ] **Step 4: Run output tests**

Run: `uv run --extra dev pytest tests/test_output.py -q`
Expected: PASS (the existing three tests still pass: stdout JSON ends with one `\n`).

- [ ] **Step 5: Write the failing CLI tests**

Append to `tests/test_read_commands.py`:

```python
def test_fields_and_format_on_a_read_command(fake):
    fake.responses["subjects"] = {
        "count": 2,
        "next": None,
        "results": [
            {"id": "s1", "name": "Alpha", "last_position": {"geometry": {"coordinates": [36.8, -1.3]}}},
            {"id": "s2", "name": "Beta, Jr"},
        ],
    }
    result = _run(["subjects", "search", "--fields", "id,name,last_position.geometry.coordinates.1"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["records"][0] == {"id": "s1", "name": "Alpha", "last_position.geometry.coordinates.1": -1.3}
    assert doc["records"][1]["last_position.geometry.coordinates.1"] is None
    assert doc["meta"]["total"] == 2

    result = _run(["subjects", "search", "--fields", "id,name", "--format", "csv"])
    assert result.output == 'id,name\ns1,Alpha\ns2,"Beta, Jr"\n'

    result = _run(["subjects", "search", "--format", "tsv"])
    assert result.exit_code == 2
    assert "--format tsv needs --fields" in result.output
```

Append to `tests/test_cli.py` (find the existing `events list event-types --json` test and add beside it):

```python
def test_list_event_types_fields_only_under_json(monkeypatch):
    fake = FakeER(event_types=[{"value": "a", "display": "A", "category": "c", "is_active": True}])
    monkeypatch.setattr(cli_mod, "_connect", lambda ctx: fake)
    runner = CliRunner()
    human = runner.invoke(main, ["events", "list", "event-types"])
    with_fields = runner.invoke(main, ["events", "list", "event-types", "--fields", "value"])
    # guardrail: --fields without --json changes nothing about the human output
    assert with_fields.output == human.output
    assert human.output.startswith("a")
    as_json = runner.invoke(main, ["events", "list", "event-types", "--json", "--fields", "value"])
    assert json.loads(as_json.output)["records"] == [{"value": "a"}]
```

(Use whatever fixture/import names `tests/test_cli.py` already uses for `FakeER`, `cli_mod`, `main`, `CliRunner`; read its header first.)

- [ ] **Step 6: Run them to verify they fail**

Run: `uv run --extra dev pytest tests/test_read_commands.py::test_fields_and_format_on_a_read_command tests/test_cli.py::test_list_event_types_fields_only_under_json -q`
Expected: FAIL with "No such option: --fields".

- [ ] **Step 7: Wire the options**

In `read_commands.py` `_make_command`:

```python
from .output import check_format, emit, output_options, parse_fields
...
    def callback(ctx, output, fields=None, fmt="json", limit=None, **kwargs):
        fields = parse_fields(fields)
        check_format(fields, fmt)
        ...
        emit(records, meta, output, fields=fields, fmt=fmt)
...
    fn = click.option("-o", "--output", ...)(fn)
    fn = output_options(fn)
```

In `cli.py` `json_output_options`, add `f = output_options(f)` after the `--json` option, and change the three callbacks' signatures to `(ctx, json_, output, fields, fmt)` (plus `category`/`value` where present). In each `if json_ or output:` branch:

```python
        fields = parse_fields(fields)
        check_format(fields, fmt)
        emit(categories, {"total": len(categories), "pages": 1}, output, fields=fields, fmt=fmt)
```

The human branch ignores `fields`/`fmt` entirely (guardrail).

- [ ] **Step 8: Run the full suite and linters**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check src tests && uv run --extra dev ruff format --check src tests`
Expected: all pass. `test_every_command_is_registered_with_output_option` still passes (it checks `output`, `limit`, `page_size` only).

- [ ] **Step 9: Commit**

```bash
git add src/earthranger_cli/output.py src/earthranger_cli/read_commands.py src/earthranger_cli/cli.py tests/test_output.py tests/test_read_commands.py tests/test_cli.py
git commit -m "Read commands: --fields projection and --format tsv|csv

The commonest thing done with the records document was piping it into
jq to pull three columns; asking for the columns removes the step and
keeps raw payloads out of an agent's context. Dotted paths index into
lists (coordinates.1 is latitude); a missing path is an empty cell.
TSV escapes tabs and newlines so column N is always column N; CSV
quotes per RFC 4180. The authoring commands gain the flags only under
--json/-o; their human output is untouched.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Page cap and repeated-`next` guard in `read.py`

**Files:**
- Modify: `src/earthranger_cli/read.py:89-148`
- Modify: `src/earthranger_cli/read_commands.py:103-115` (`_all_event_types` unpacks the new tuple)
- Test: `tests/test_read.py`

**Interfaces:**
- Produces:
  ```python
  MAX_PAGES = 200
  def follow_pages(client, page, *, limit=None) -> tuple[list, int, int | None, bool]   # + truncated
  def fetch(...) -> tuple[list, dict]   # meta gains "truncated": True and "note" when the walk stopped early
  def fetch_count(client, path, params=None, *, version=None, unwrap=None) -> int | None
  def fetch_text(client, path, params=None) -> tuple[str, str]   # (body, content_type)
  ```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_read.py` (reuse the file's existing fake-client helper for `_get`; read its header first):

```python
def test_follow_pages_stops_at_max_pages_and_marks_truncated(monkeypatch):
    from earthranger_cli import read

    monkeypatch.setattr(read, "MAX_PAGES", 3)
    client = _Client({f"p{i}": {"results": [{"i": i}], "next": f"p{i + 1}"} for i in range(1, 10)})
    records, pages, _, truncated = read.follow_pages(client, {"results": [{"i": 0}], "next": "p1"})
    assert [r["i"] for r in records] == [0, 1, 2]
    assert pages == 3
    assert truncated is True


def test_follow_pages_stops_when_next_repeats():
    client = _Client({"loop": {"results": [{"i": 1}], "next": "loop"}})
    records, pages, _, truncated = read.follow_pages(client, {"results": [{"i": 0}], "next": "loop"})
    assert [r["i"] for r in records] == [0, 1]
    assert pages == 2
    assert truncated is True


def test_fetch_reports_truncation_in_meta(monkeypatch):
    monkeypatch.setattr(read, "MAX_PAGES", 1)
    client = _Client({"things": {"count": 50, "results": [{"i": 0}], "next": "p1"}, "p1": {"results": []}})
    records, meta = read.fetch(client, "things", paginate=True)
    assert meta["truncated"] is True
    assert "200" not in meta["note"] and "page" in meta["note"]
    assert meta["count_reported"] == 50


def test_fetch_count_asks_for_one_record():
    client = _Client({"things": {"count": 1234, "results": [{"i": 0}], "next": "p1"}})
    assert read.fetch_count(client, "things", {"state": "active"}) == 1234
    assert client.calls[-1][1]["page_size"] == 1
    assert client.calls[-1][1]["state"] == "active"


def test_fetch_count_is_none_for_unpaginated_endpoints():
    client = _Client({"things": [{"i": 0}, {"i": 1}]})
    assert read.fetch_count(client, "things", {}) is None
```

If `tests/test_read.py` has no reusable fake, add one at the top of the file:

```python
class _Client:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def _api_root(self, version="v1.0"):
        return f"https://fake.pamdas.org/api/{version}"

    def _get(self, path, base_url=None, params=None, max_retries=0, **kwargs):
        self.calls.append((path, params or {}))
        key = path.rsplit("/", 1)[-1] if path.startswith("http") else path
        return self.responses[key]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_read.py -q`
Expected: FAIL — `follow_pages` returns a 3-tuple; `fetch_count` missing.

- [ ] **Step 3: Implement**

In `read.py`:

```python
MAX_PAGES = 200  # otus's ceiling; ER's largest sites page far below this


def follow_pages(client, page, *, limit=None) -> tuple[list, int, int | None, bool]:
    """... Returns (records, pages_fetched, count_reported, truncated). `truncated`
    is True when the walk stopped at MAX_PAGES or because a `next` link repeated
    (a server bug that would otherwise loop forever); the caller says so in meta."""
    records, next_url, count = normalize_page(page)
    pages = 1
    seen = set()
    truncated = False
    while next_url and (limit is None or len(records) < limit):
        if pages >= MAX_PAGES or next_url in seen:
            truncated = True
            break
        seen.add(next_url)
        link = urlsplit(next_url)
        if link.netloc:
            origin = urlsplit(client._api_root())
            next_url = urlunsplit((origin.scheme, origin.netloc, link.path, link.query, ""))
        more, next_url, _ = normalize_page(get_json(client, next_url))
        records.extend(more)
        pages += 1
    if limit is not None:
        records = records[:limit]
    return records, pages, count, truncated
```

In `fetch`, unpack four values and after building `meta`:

```python
    if truncated:
        meta["truncated"] = True
        meta["note"] = (
            f"stopped after {pages} page(s); the result is a floor, not the total. "
            "Narrow the query (--since/--until, --limit) or raise --page-size."
        )
```

(`truncated = False` in the non-paginated branch.) Add:

```python
def fetch_count(client, path, params=None, *, version=None, unwrap=None) -> int | None:
    """The server's own total for a query in one request: DRF reports `count`
    beside the first page, so ask for one record and read the envelope. None
    when the endpoint is a bare list or a single object (no count to read)."""
    params = {k: v for k, v in (params or {}).items() if v is not None}
    params["page_size"] = 1
    base_url = client._api_root(version) if version else None
    page = get_json(client, path, base_url=base_url, params=params)
    if unwrap is not None:
        page = unwrap(page)
    if isinstance(page, dict) and "results" in page:
        return page.get("count")
    return None


def fetch_text(client, path, params=None) -> tuple[str, str]:
    """GET a non-JSON body (the CSV exports). Returns (text, content_type).
    erclient's `return_response=True` hands back the raw response on 2xx and
    still raises its typed errors on 401/403/404."""
    params = {k: v for k, v in (params or {}).items() if v is not None}
    response = client._get(path, max_retries=0, params=params, return_response=True)
    return response.text, response.headers.get("Content-Type", "")
```

Update `_all_event_types` in `read_commands.py`: `v2, _, _, _ = follow_pages(client, v2_page)`.

- [ ] **Step 4: Run tests and linters**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/earthranger_cli/read.py src/earthranger_cli/read_commands.py tests/test_read.py
git commit -m "read: page cap, repeated-next guard, count and raw-text fetches

follow_pages now stops at 200 pages or on a next link it has already
seen and reports the walk as truncated; fetch puts that in meta so a
caller never mistakes a floor for a total. fetch_count asks for one
record and reads DRF's count; fetch_text returns a CSV body unparsed.
Both back the --count-only and export commands that follow.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `clock.py` — site timezone, server clock, day/last bounds, period buckets

**Files:**
- Create: `src/earthranger_cli/clock.py`
- Modify: `tests/conftest.py` (FakeER: `return_response=True` support, `status` body, `Date` header)
- Test: `tests/test_clock.py`

**Interfaces:**
- Produces:
  ```python
  def site_tz(info: dict) -> tzinfo | None
  def parse_duration(raw: str) -> timedelta          # "30m" "50h" "7d" "2w"; ValueError otherwise
  def last_bounds(info: dict, span: timedelta) -> tuple[str, str] | None
  def day_bounds(info: dict, days_ago: int = 0) -> tuple[str, str] | None
  def fetch_clock(client) -> dict   # {"utc","timezone_name","timezone","local","today": {"since","until"}|None}
  def clock_meta(info: dict) -> dict  # {"server_utc", "site_now", "site_tz"} with None values dropped
  def bucket_window(since: str, until: str, period: str, tz) -> list[tuple[str, str, str]]  # (label, lo, hi)
  def parse_ts(value) -> datetime | None            # ISO-8601 with Z or offset; naive -> UTC
  ```
- FakeER gains `self.status = {"server_timezone_name": "Africa/Nairobi", "server_timezone": "EAT"}` and `self.date_header = "Fri, 09 Oct 2026 09:00:00 GMT"`; `_get(path, return_response=True)` returns a `FakeResponse` whose `.text` is `json.dumps({"data": body, "status": {"code": 200}})`, `.headers = {"Date": self.date_header, "Content-Type": "application/json"}`, `.status_code = 200`; the call is recorded as `("_get_response", path, params)` so index-based `_gets()` assertions in existing tests are unaffected. For `path == "status"` the body is `self.responses.get("status", self.status)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_clock.py`:

```python
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import FakeER
from earthranger_cli import clock


def test_site_tz_prefers_iana_name_then_offset_then_none():
    assert clock.site_tz({"timezone_name": "Africa/Nairobi"}) == ZoneInfo("Africa/Nairobi")
    assert clock.site_tz({"timezone_name": None, "timezone": "+03"}) == timezone(timedelta(hours=3))
    assert clock.site_tz({"timezone": "-0545"}) == timezone(-timedelta(hours=5, minutes=45))
    # Review Focus 3: an abbreviation alone is not a timezone
    assert clock.site_tz({"timezone_name": None, "timezone": "EAT"}) is None
    assert clock.site_tz({}) is None


@pytest.mark.parametrize("raw,expected", [("30m", timedelta(minutes=30)), ("50h", timedelta(hours=50)), ("7d", timedelta(days=7)), ("2w", timedelta(weeks=2))])
def test_parse_duration(raw, expected):
    assert clock.parse_duration(raw) == expected


@pytest.mark.parametrize("raw", ["7", "7x", "", "d7", "1.5h"])
def test_parse_duration_refuses_other_forms(raw):
    with pytest.raises(ValueError, match="count and a unit"):
        clock.parse_duration(raw)


def test_day_bounds_are_the_site_calendar_day():
    info = {"utc": "2026-10-09T02:30:00Z", "timezone_name": "America/Los_Angeles"}
    assert clock.day_bounds(info) == ("2026-10-08T00:00:00-07:00", "2026-10-08T23:59:59-07:00")
    assert clock.day_bounds(info, 1) == ("2026-10-07T00:00:00-07:00", "2026-10-07T23:59:59-07:00")
    assert clock.day_bounds({"utc": "2026-10-09T02:30:00Z", "timezone": "EAT"}) is None


def test_last_bounds_subtracts_in_utc_across_dst():
    # Review Focus 2: US fall-back is 2026-11-01 09:00Z; 24h before 2026-11-01T20:00Z is a 25-hour local gap
    info = {"utc": "2026-11-01T20:00:00Z", "timezone_name": "America/Los_Angeles"}
    since, until = clock.last_bounds(info, timedelta(hours=24))
    assert until == "2026-11-01T12:00:00-08:00"
    assert since == "2026-10-31T13:00:00-07:00"


def test_fetch_clock_reads_date_header_and_status_body():
    fake = FakeER()
    info = clock.fetch_clock(fake)
    assert info["utc"] == "2026-10-09T09:00:00Z"
    assert info["timezone_name"] == "Africa/Nairobi"
    assert info["local"] == "2026-10-09T12:00:00+03:00"
    assert info["today"] == {"since": "2026-10-09T00:00:00+03:00", "until": "2026-10-09T23:59:59+03:00"}
    assert fake.calls == [("_get_response", "status", None)]
    assert clock.clock_meta(info) == {
        "server_utc": "2026-10-09T09:00:00Z",
        "site_now": "2026-10-09T12:00:00+03:00",
        "site_tz": "Africa/Nairobi",
    }


def test_fetch_clock_without_usable_timezone_still_has_utc():
    fake = FakeER()
    fake.status = {"server_timezone": "EAT"}
    info = clock.fetch_clock(fake)
    assert info["utc"] == "2026-10-09T09:00:00Z"
    assert info["local"] is None and info["today"] is None
    assert clock.clock_meta(info) == {"server_utc": "2026-10-09T09:00:00Z", "site_tz": "EAT"}


def test_bucket_window_days_weeks_months():
    tz = ZoneInfo("Africa/Nairobi")
    days = clock.bucket_window("2026-10-07T10:00:00+03:00", "2026-10-09T12:00:00+03:00", "day", tz)
    assert [b[0] for b in days] == ["2026-10-07", "2026-10-08", "2026-10-09"]
    assert days[0][1:] == ("2026-10-07T00:00:00+03:00", "2026-10-07T23:59:59+03:00")
    weeks = clock.bucket_window("2026-10-07T00:00:00+03:00", "2026-10-13T00:00:00+03:00", "week", tz)
    assert [b[0] for b in weeks] == ["2026-10-05", "2026-10-12"]  # Monday-start weeks
    months = clock.bucket_window("2026-08-20T00:00:00+03:00", "2026-10-01T00:00:00+03:00", "month", tz)
    assert [b[0] for b in months] == ["2026-08", "2026-09", "2026-10"]
    assert months[1][1:] == ("2026-09-01T00:00:00+03:00", "2026-09-30T23:59:59+03:00")
    with pytest.raises(ValueError, match="period"):
        clock.bucket_window("2026-10-07T00:00:00+03:00", "2026-10-08T00:00:00+03:00", "fortnight", tz)


def test_parse_ts_accepts_z_offset_and_naive():
    assert clock.parse_ts("2026-10-09T09:00:00Z").utcoffset() == timedelta(0)
    assert clock.parse_ts("2026-10-09T12:00:00+03:00").hour == 12
    assert clock.parse_ts("2026-10-09T09:00:00").tzinfo == timezone.utc
    assert clock.parse_ts(None) is None and clock.parse_ts("garbage") is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_clock.py -q`
Expected: ModuleNotFoundError `earthranger_cli.clock`.

- [ ] **Step 3: Extend FakeER**

In `tests/conftest.py`, add near the top:

```python
import json
from dataclasses import dataclass, field


@dataclass
class FakeResponse:
    body: object
    status_code: int = 200
    content_type: str = "application/json"
    date: str = "Fri, 09 Oct 2026 09:00:00 GMT"
    text_override: str | None = None

    @property
    def text(self) -> str:
        if self.text_override is not None:
            return self.text_override
        return json.dumps({"data": self.body, "status": {"code": self.status_code}})

    @property
    def headers(self) -> dict:
        return {"Date": self.date, "Content-Type": self.content_type}
```

In `FakeER.__init__` add `self.status = {"server_timezone_name": "Africa/Nairobi", "server_timezone": "EAT"}` and `self.date_header = "Fri, 09 Oct 2026 09:00:00 GMT"`. At the top of `_get`, before the `user/me` branch:

```python
        if kwargs.get("return_response"):
            self.calls.append(("_get_response", path, params))
            if path == "status":
                body = self.responses.get("status", self.status)
            else:
                body = self.responses[path]
            if isinstance(body, FakeResponse):
                return body
            return FakeResponse(body, date=self.date_header)
```

- [ ] **Step 4: Implement `clock.py`**

```python
"""The site's clock: server UTC time, site timezone, and the windows built from them.

"Today" is a question about the site's calendar, not the caller's. A site on
America/Los_Angeles rolls over seven hours after UTC does, so a UTC-day window
reports yesterday evening as today and misses everything after 17:00 local.
Everything here is computed once per invocation from one GET /status.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .read import READ_RETRIES  # noqa: F401  (same transport policy; documents the dependency)

# das's `server_timezone` is `timezone.localtime().strftime("%Z")`: an abbreviation
# ("EAT", "PDT") or, for zones tzdb gives none, a bare offset ("+03", "+0545").
# Only the offset form is usable without a tz database.
_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})?$")
_DURATION_RE = re.compile(r"^(\d+)([mhdw])$")
_UNIT = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
PERIODS = ("day", "week", "month")


def site_tz(info: dict):
    name = info.get("timezone_name")
    if name:
        try:
            return ZoneInfo(str(name))
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    m = _OFFSET_RE.match(str(info.get("timezone") or "").strip())
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        return timezone(-delta if m.group(1) == "-" else delta)
    return None


def parse_duration(raw: str) -> timedelta:
    m = _DURATION_RE.match((raw or "").strip())
    if not m:
        raise ValueError(f"--last takes a count and a unit — 30m, 50h, 7d, 2w — not {raw!r}")
    return timedelta(**{_UNIT[m.group(2)]: int(m.group(1))})


def parse_ts(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _now_utc(info: dict) -> datetime | None:
    return parse_ts(info.get("utc"))


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def last_bounds(info: dict, span: timedelta) -> tuple[str, str] | None:
    """[now − span, now] in site time. Subtract in UTC, then convert: local
    arithmetic is wall-clock arithmetic and a window across a DST change came
    out an hour long or short."""
    tz, now = site_tz(info), _now_utc(info)
    if tz is None or now is None:
        return None
    return _iso((now - span).astimezone(tz)), _iso(now.astimezone(tz))


def day_bounds(info: dict, days_ago: int = 0) -> tuple[str, str] | None:
    """The site-local calendar day, ending 23:59:59 so two days never both
    claim an event filed exactly at midnight."""
    tz, now = site_tz(info), _now_utc(info)
    if tz is None or now is None:
        return None
    start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start -= timedelta(days=days_ago)
    return _iso(start), _iso(start + timedelta(days=1) - timedelta(seconds=1))


def fetch_clock(client) -> dict:
    """One GET /status: UTC from the HTTP Date header (the body carries no
    timestamp), the site's timezone from the body."""
    response = client._get("status", max_retries=0, return_response=True)
    date_hdr = response.headers.get("Date")
    now = parsedate_to_datetime(date_hdr).astimezone(UTC) if date_hdr else datetime.now(UTC)
    info: dict = {
        "utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timezone_name": None,
        "timezone": None,
        "local": None,
        "today": None,
    }
    try:
        body = json.loads(response.text)
        if isinstance(body, dict) and "data" in body:
            body = body["data"]
        if isinstance(body, dict):
            info["timezone_name"] = body.get("server_timezone_name")
            info["timezone"] = body.get("server_timezone")
    except ValueError:
        pass
    tz = site_tz(info)
    if tz is not None:
        info["local"] = _iso(now.astimezone(tz))
        since, until = day_bounds(info)
        info["today"] = {"since": since, "until": until}
    return info


def clock_meta(info: dict) -> dict:
    meta = {
        "server_utc": info.get("utc"),
        "site_now": info.get("local"),
        "site_tz": info.get("timezone_name") or info.get("timezone"),
    }
    return {k: v for k, v in meta.items() if v}


def bucket_window(since: str, until: str, period: str, tz) -> list[tuple[str, str, str]]:
    """Calendar buckets covering [since, until] in site time: (label, lo, hi)
    with hi = next start − 1 s. Days label YYYY-MM-DD, weeks their Monday,
    months YYYY-MM."""
    if period not in PERIODS:
        raise ValueError(f"period must be one of {', '.join(PERIODS)}, not {period!r}")
    lo, hi = parse_ts(since), parse_ts(until)
    if lo is None or hi is None:
        raise ValueError("--since/--until must be ISO-8601 timestamps to bucket by period")
    cur = lo.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end = hi.astimezone(tz)
    if period == "week":
        cur -= timedelta(days=cur.weekday())
    elif period == "month":
        cur = cur.replace(day=1)
    buckets = []
    while cur <= end:
        if period == "day":
            nxt, label = cur + timedelta(days=1), cur.strftime("%Y-%m-%d")
        elif period == "week":
            nxt, label = cur + timedelta(days=7), cur.strftime("%Y-%m-%d")
        else:
            nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
            label = cur.strftime("%Y-%m")
        buckets.append((label, _iso(cur), _iso(nxt - timedelta(seconds=1))))
        cur = nxt
    return buckets
```

(Drop the `READ_RETRIES` import line if ruff flags it; it is only documentation.)

- [ ] **Step 5: Run tests and linters**

Run: `uv run --extra dev pytest tests/test_clock.py tests/test_read_commands.py -q && uv run --extra dev ruff check src tests && uv run --extra dev ruff format --check src tests`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/earthranger_cli/clock.py tests/test_clock.py tests/conftest.py
git commit -m "clock: site timezone, server clock, day/last bounds, period buckets

One GET /status gives the server's UTC time (Date header) and the
site's timezone (server_timezone_name, falling back to an offset in
server_timezone). From it: the site's calendar day, [now - span, now]
computed in UTC so DST cannot stretch it, and day/week/month buckets.
An abbreviation alone is not a timezone: callers refuse rather than
silently answer in UTC.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `er now` and site-time fields in every read command's meta

**Files:**
- Modify: `src/earthranger_cli/read_commands.py` (`_make_command` callback; new `now` command in `register`)
- Test: `tests/test_read_commands.py`

**Interfaces:**
- Consumes: `clock.fetch_clock`, `clock.clock_meta`.
- Produces: `read_commands.get_clock(ctx, client, *, required: bool) -> dict | None` — fetches once per invocation and caches in `ctx.obj["clock"]`; with `required=False` swallows `ERClientException`/`requests.exceptions.RequestException` and returns None; with `required=True` re-raises. Used by Tasks 5 and 6.

- [ ] **Step 1: Write the failing tests**

```python
def test_meta_carries_the_site_clock(fake):
    fake.responses["regions"] = [{"id": "r1"}]
    doc = json.loads(_run(["regions", "list"]).output)
    assert doc["meta"]["server_utc"] == "2026-10-09T09:00:00Z"
    assert doc["meta"]["site_now"] == "2026-10-09T12:00:00+03:00"
    assert doc["meta"]["site_tz"] == "Africa/Nairobi"
    # the clock is one extra request, after the data request
    assert [c[0] for c in fake.calls if c[0] in ("_get", "_get_response")] == ["_get", "_get_response"]


def test_meta_clock_is_best_effort(fake, monkeypatch):
    from erclient.er_errors import ERClientException

    fake.responses["regions"] = [{"id": "r1"}]

    def boom(*a, **k):
        raise ERClientException("status down")

    monkeypatch.setattr("earthranger_cli.clock.fetch_clock", boom)
    result = _run(["regions", "list"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert "server_utc" not in doc["meta"] and doc["records"] == [{"id": "r1"}]


def test_meta_clock_omits_site_fields_without_a_usable_timezone(fake):
    fake.status = {"server_timezone": "EAT"}  # Review Focus 3
    fake.responses["regions"] = []
    meta = json.loads(_run(["regions", "list"]).output)["meta"]
    assert meta["server_utc"] == "2026-10-09T09:00:00Z"
    assert "site_now" not in meta and meta["site_tz"] == "EAT"


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
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_read_commands.py -k "clock or now_prints" -q`
Expected: FAIL (KeyError `server_utc`; "No such command 'now'").

- [ ] **Step 3: Implement**

In `read_commands.py`:

```python
import requests
from erclient.er_errors import ERClientException

from . import clock as _clock


def get_clock(ctx, client, *, required: bool) -> dict | None:
    """The site clock, fetched once per invocation. Passive callers (meta
    enrichment) get None when /status fails; a window flag that needs the
    clock passes required=True and lets the error surface."""
    cached = ctx.obj.get("clock")
    if cached is not None:
        return cached
    try:
        info = _clock.fetch_clock(client)
    except (ERClientException, requests.exceptions.RequestException):
        if required:
            raise
        return None
    ctx.obj["clock"] = info
    return info
```

In the callback, after `records, meta = fetch(...)`:

```python
        info = get_clock(ctx, client, required=False)
        if info:
            meta.update(_clock.clock_meta(info))
```

Add the `now` command in `register` (after the loop):

```python
    @click.command("now", help="The server's current time (UTC), the site's local time and timezone, "
                              "and today's --since/--until bounds in site time.\n\n[GET /api/v1.0/status]")
    @click.option("-o", "--output", type=click.Path(dir_okay=False), help="Write JSON here instead of stdout.")
    @output_options
    @click.pass_context
    def now(ctx, output, fields, fmt):
        fields = parse_fields(fields)
        check_format(fields, fmt)
        client = deps.connect(ctx)
        info = get_clock(ctx, client, required=True)
        record = {
            "utc": info["utc"],
            "site_now": info["local"],
            "site_tz": info.get("timezone_name") or info.get("timezone"),
            "today": info["today"],
        }
        emit([record], {"total": 1, "pages": 1}, output, fields=fields, fmt=fmt)

    main.add_command(deps.connection_options(deps.api_errors(now)))
```

(Check decorator order against the registry commands: `api_errors` wraps the callback *inside* `pass_context`; mirror `_make_command` exactly — build the function, then `deps.api_errors`, then `click.pass_context`, then options, then `click.command`.)

- [ ] **Step 4: Run the suite; fix tests that counted `_get` calls**

Run: `uv run --extra dev pytest -q`
Expected: PASS. If any test asserts on `fake.calls` as a whole (not via `_gets`), filter `_get_response` entries there.

- [ ] **Step 5: Commit**

```bash
git add src/earthranger_cli/read_commands.py tests/test_read_commands.py
git commit -m "Read commands: er now, and the site clock in every meta

Agents repeatedly ran a status call and did timezone arithmetic by
hand. Every records document now carries server_utc, site_now and
site_tz (one cached GET /status per invocation, best-effort), and
er now prints today's bounds ready to paste into --since/--until.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Windows — `--since/--until` folding, `--today`, `--yesterday`, `--last`

**Files:**
- Create: `src/earthranger_cli/windows.py`
- Modify: `src/earthranger_cli/read_commands.py` (`ReadCommand.window`, `ReadCommand.default_window`, rows for events/patrols/observations/tracks, callback wiring; `_prepare_observations` loses its since-defaulting)
- Test: `tests/test_windows.py`, `tests/test_read_commands.py`

**Interfaces:**
- `ReadCommand` gains `window: str | None = None` — `"filter"` (fold into `filter` JSON `date_range.lower/upper`: events, patrols), `"since_until"` (query params: observations, tracks), `"after_before"` (`after_date`/`before_date`: observations export, Task 7) — and `default_window: timedelta | None = None` (observations: 24 h).
- `windows.py` produces:
  ```python
  @dataclass(frozen=True)
  class WindowRequest:
      since: str | None
      until: str | None
      mode: str | None        # None | "today" | "yesterday" | "last"
      span: timedelta | None  # for "last"

  def window_options(f)                                   # --since --until --today --yesterday --last
  def parse_window(kwargs: dict) -> WindowRequest         # pops the five keys; UsageError on conflicts/bad --last
  def inclusive_until(until: str | None) -> str | None    # bare date -> T23:59:59.999999
  def resolve_window(req, *, get_info, default_window, note) -> tuple[str | None, str | None, dict | None]
      # get_info: zero-arg callable returning the clock dict (called only when mode is set)
      # returns (since, until, window_meta); raises click.ClickException when mode set and no tz
  def apply_window(kind: str, params: dict, since, until) -> None   # folds per kind; UsageError on bad --filter
  ```

- [ ] **Step 1: Write the failing unit tests**

Create `tests/test_windows.py`:

```python
import json
from datetime import timedelta

import click
import pytest

from earthranger_cli import windows

CLOCK = {"utc": "2026-10-09T09:00:00Z", "timezone_name": "Africa/Nairobi", "timezone": "EAT"}


def test_parse_window_pops_flags_and_rejects_conflicts():
    kw = {"since": "2026-10-01", "until": None, "today": False, "yesterday": False, "last": None, "x": 1}
    req = windows.parse_window(kw)
    assert (req.since, req.until, req.mode) == ("2026-10-01", None, None)
    assert kw == {"x": 1}
    with pytest.raises(click.UsageError, match="only one of --today, --yesterday, --last"):
        windows.parse_window({"since": None, "until": None, "today": True, "yesterday": True, "last": None})
    with pytest.raises(click.UsageError, match="--last sets the whole window; drop --since"):
        windows.parse_window({"since": "2026-10-01", "until": None, "today": False, "yesterday": False, "last": "7d"})
    with pytest.raises(click.UsageError, match="count and a unit"):
        windows.parse_window({"since": None, "until": None, "today": False, "yesterday": False, "last": "7"})


def test_inclusive_until_extends_a_bare_date_only():
    assert windows.inclusive_until("2026-08-31") == "2026-08-31T23:59:59.999999"  # Review Focus 1
    assert windows.inclusive_until("2026-08-31T10:00:00Z") == "2026-08-31T10:00:00Z"
    assert windows.inclusive_until(None) is None


def test_resolve_window_today_and_last_use_the_clock():
    req = windows.WindowRequest(None, None, "today", None)
    since, until, meta = windows.resolve_window(req, get_info=lambda: CLOCK, default_window=None, note=None)
    assert (since, until) == ("2026-10-09T00:00:00+03:00", "2026-10-09T23:59:59+03:00")
    assert meta == {"since": since, "until": until, "tz": "Africa/Nairobi", "mode": "today"}
    req = windows.WindowRequest(None, None, "last", timedelta(hours=2))
    since, until, meta = windows.resolve_window(req, get_info=lambda: CLOCK, default_window=None, note=None)
    assert (since, until) == ("2026-10-09T10:00:00+03:00", "2026-10-09T12:00:00+03:00")
    assert meta["mode"] == "last"


def test_resolve_window_refuses_without_a_timezone():
    req = windows.WindowRequest(None, None, "today", None)
    with pytest.raises(click.ClickException, match="no usable timezone"):
        windows.resolve_window(req, get_info=lambda: {"utc": "2026-10-09T09:00:00Z", "timezone": "EAT"}, default_window=None, note=None)


def test_resolve_window_default_fills_since_and_notes(capsys):
    req = windows.WindowRequest(None, "2026-10-09T12:00:00Z", None, None)
    since, until, meta = windows.resolve_window(
        req, get_info=lambda: CLOCK, default_window=timedelta(hours=24), note=lambda s: click.echo(s, err=True)
    )
    assert since == "2026-10-08T12:00:00Z" and until == "2026-10-09T12:00:00Z"
    assert "defaulting to the 24 hours before --until 2026-10-09T12:00:00Z" in capsys.readouterr().err
    assert meta == {"since": since, "until": until}


def test_resolve_window_explicit_passthrough_has_meta_without_tz():
    req = windows.WindowRequest("2026-10-01", "2026-10-02T00:00:00Z", None, None)
    assert windows.resolve_window(req, get_info=lambda: CLOCK, default_window=None, note=None) == (
        "2026-10-01", "2026-10-02T00:00:00Z", {"since": "2026-10-01", "until": "2026-10-02T00:00:00Z"}
    )
    assert windows.resolve_window(windows.WindowRequest(None, None, None, None), get_info=None, default_window=None, note=None) == (None, None, None)


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
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_windows.py -q`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `windows.py`**

```python
"""Time windows for the read commands, in the site's calendar.

`--since/--until` are the caller's exact bounds; `--today`, `--yesterday` and
`--last 7d` are computed from the site clock (clock.py) so an agent never does
timezone arithmetic by hand. Each endpoint spells the window differently:
events and patrols take it inside the `filter` JSON, observations and tracks
as `since`/`until`, the observations export as `after_date`/`before_date`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import click

from . import clock

_BARE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KINDS = ("filter", "since_until", "after_before")


@dataclass(frozen=True)
class WindowRequest:
    since: str | None
    until: str | None
    mode: str | None
    span: timedelta | None


def window_options(f):
    f = click.option("--last", metavar="DURATION", help="The last 30m / 50h / 7d / 2w, ending now, in site time.")(f)
    f = click.option("--yesterday", is_flag=True, help="The site's previous calendar day.")(f)
    f = click.option("--today", is_flag=True, help="The site's current calendar day (its timezone, not UTC).")(f)
    f = click.option("--until", help="ISO-8601 end; a bare date means the end of that day.")(f)
    f = click.option("--since", help="ISO-8601 start.")(f)
    return f


def parse_window(kwargs: dict) -> WindowRequest:
    since, until = kwargs.pop("since", None), kwargs.pop("until", None)
    today, yesterday, last = kwargs.pop("today", False), kwargs.pop("yesterday", False), kwargs.pop("last", None)
    chosen = [n for n, v in (("--today", today), ("--yesterday", yesterday), ("--last", last)) if v]
    if len(chosen) > 1:
        raise click.UsageError("pass only one of --today, --yesterday, --last.")
    if chosen and (since or until):
        clash = "--since" if since else "--until"
        raise click.UsageError(f"{chosen[0]} sets the whole window; drop {clash}.")
    span = None
    if last:
        try:
            span = clock.parse_duration(last)
        except ValueError as e:
            raise click.UsageError(str(e)) from None
    mode = chosen[0][2:] if chosen else None
    return WindowRequest(since, inclusive_until(until), mode, span)


def inclusive_until(until: str | None) -> str | None:
    """ER reads `2026-08-31` as midnight *opening* the 31st, so --until on a bare
    date left the whole day out. Anything with a time in it is left alone."""
    if until and _BARE_DATE.match(until.strip()):
        return f"{until.strip()}T23:59:59.999999"
    return until


def resolve_window(
    req: WindowRequest,
    *,
    get_info: Callable[[], dict] | None,
    default_window: timedelta | None,
    note: Callable[[str], None] | None,
) -> tuple[str | None, str | None, dict | None]:
    since, until = req.since, req.until
    meta: dict | None = None
    if req.mode:
        info = get_info()
        if req.mode == "last":
            bounds = clock.last_bounds(info, req.span)
        else:
            bounds = clock.day_bounds(info, 1 if req.mode == "yesterday" else 0)
        if bounds is None:
            reported = info.get("timezone_name") or info.get("timezone") or "none"
            raise click.ClickException(
                f"the site reported no usable timezone ({reported}); pass --since/--until explicitly."
            )
        since, until = bounds
        meta = {"since": since, "until": until, "tz": info.get("timezone_name") or info.get("timezone"), "mode": req.mode}
    elif since is None and default_window is not None:
        end = clock.parse_ts(until) if until else None
        if until and end is None:
            raise click.UsageError(f"--until must be an ISO-8601 timestamp, got {until!r}.")
        from datetime import UTC, datetime

        end = end or datetime.now(UTC)
        since = (end - default_window).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        hours = int(default_window.total_seconds() // 3600)
        anchor = f"--until {until}" if until else "now"
        if note:
            note(f"note: no --since given; defaulting to the {hours} hours before {anchor} ({since}).")
    if since is not None or until is not None:
        meta = meta or {"since": since, "until": until}
    return since, until, meta


def apply_window(kind: str, params: dict, since, until) -> None:
    if since is None and until is None:
        return
    if kind == "filter":
        raw = params.get("filter")
        try:
            current = json.loads(raw) if raw else {}
        except json.JSONDecodeError as e:
            raise click.UsageError(f"--filter is not valid JSON ({e}); --since/--until merge into it.") from None
        if not isinstance(current, dict):
            raise click.UsageError("--filter must be a JSON object for --since/--until to merge into.")
        window = dict(current.get("date_range") or {})
        if since is not None:
            window["lower"] = since
        if until is not None:
            window["upper"] = until
        current["date_range"] = window
        params["filter"] = json.dumps(current, separators=(",", ":"))
        return
    lower, upper = ("since", "until") if kind == "since_until" else ("after_date", "before_date")
    if since is not None:
        params[lower] = since
    if until is not None:
        params[upper] = until
```

(Move the `datetime` import to the top; keep ruff happy. The default-window message must stay byte-identical to the existing observations note so `tests/test_read_commands.py`'s current assertions hold: check them with `grep -n "defaulting" tests/test_read_commands.py` and match.)

- [ ] **Step 4: Run unit tests**

Run: `uv run --extra dev pytest tests/test_windows.py -q`
Expected: PASS.

- [ ] **Step 5: Write the failing command-level tests**

Append to `tests/test_read_commands.py`:

```python
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
    assert _gets(fake)[0][2] == {"since": "2026-10-08T00:00:00+03:00", "until": "2026-10-08T23:59:59+03:00"}
    fake.responses["observations"] = {"count": 0, "next": None, "results": []}
    result = _run(["observations", "search", "--subject-id", "s1", "--last", "24h"])
    assert result.exit_code == 0, result.output
    sent = _gets(fake)[1][2]
    assert sent["since"] == "2026-10-08T12:00:00+03:00" and sent["until"] == "2026-10-09T12:00:00+03:00"
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
```

Also update the existing observations tests: the default-since note and the `since` param format are unchanged, but the selector check still runs pre-connect — keep `test_observations_...` assertions as they are and run them.

- [ ] **Step 6: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_read_commands.py -k "today or last or window or bare_until" -q`
Expected: FAIL with "No such option: --today".

- [ ] **Step 7: Wire into `read_commands.py`**

Add fields to `ReadCommand`:

```python
    window: str | None = None  # windows.KINDS: how --since/--until reach this endpoint
    default_window: timedelta | None = None  # fill --since this far before --until when neither window flag is given
```

Rows: `events search` → `window="filter"`; `patrols search` → `window="filter"`; `observations search` → `window="since_until", default_window=OBSERVATIONS_DEFAULT_WINDOW` and **remove** its `since`/`until` `Flag`s; `tracks get` → `window="since_until"` and remove its `since`/`until` `Flag`s. Shorten `_prepare_observations` to the two selector checks (delete the `if not params.get("since")` block; `_parse_iso` becomes unused — delete it too).

In `_make_command`:

```python
from . import windows as _windows
...
    def callback(ctx, output, fields=None, fmt="json", limit=None, **kwargs):
        fields = parse_fields(fields)
        check_format(fields, fmt)
        win = _windows.parse_window(kwargs) if spec.window else None
        ... build params, run spec.prepare ...
        client = deps.connect(ctx)
        window_meta = None
        if win is not None:
            since, until, window_meta = _windows.resolve_window(
                win,
                get_info=lambda: get_clock(ctx, client, required=True),
                default_window=spec.default_window,
                note=lambda s: click.echo(s, err=True),
            )
            _windows.apply_window(spec.window, params, since, until)
        if spec.resolve is not None: ...
        records, meta = fetch(...)
        if window_meta:
            meta["window"] = window_meta
        info = get_clock(ctx, client, required=False)
        ...
```

Options: after the `for flag in reversed(spec.flags)` loop add `if spec.window: fn = _windows.window_options(fn)`.

Update `test_every_command_is_registered_with_output_option` to also assert `("since" in names) == (spec.window is not None)`.

- [ ] **Step 8: Run the full suite and linters**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check src tests && uv run --extra dev ruff format --check src tests`
Expected: PASS. Existing observations tests that assert the `since` default and the stderr note must still pass unchanged; if the note text differs, fix the text in `windows.py`, not the test.

- [ ] **Step 9: Commit**

```bash
git add src/earthranger_cli/windows.py src/earthranger_cli/read_commands.py tests/test_windows.py tests/test_read_commands.py
git commit -m "Read commands: --since/--until, --today, --yesterday, --last in site time

Events and patrols take the window inside the filter JSON (merged with
any --filter given), observations and tracks as since/until. --today
and --yesterday are the site's calendar day, --last 7d is now minus the
span computed in UTC; all three refuse when the site reports no usable
timezone rather than answering in UTC. A bare --until date means the
end of that day. meta.window records what was sent.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `--count-only` and `--group-by FIELD|day|week|month`

**Files:**
- Create: `src/earthranger_cli/aggregate.py`
- Modify: `src/earthranger_cli/read_commands.py` (`ReadCommand.time_field`; options on list commands; callback branch)
- Test: `tests/test_aggregate.py`, `tests/test_read_commands.py`

**Interfaces:**
- `ReadCommand` gains `time_field: str | None = None`: events `"time"`, observations `"recorded_at"`, patrols `"patrol_segments.0.time_range.start_time"`.
- `aggregate.py` produces:
  ```python
  def group_counts(records: list, field: str) -> tuple[list[dict], int]      # rows {group, count}, matched
  def scalar_keys(records: list, limit: int = 25) -> list[str]
  def group_by_period(records, *, period, since, until, tz, time_field) -> list[dict]   # rows {period, since, until, count}
  ```
- Callback semantics: `--count-only` → one `fetch_count` request (plus the clock); if the endpoint has no count, walk pages and report a floor. Emits `records=[{"count": N}]`, `meta={"total": 1, "pages": P, "count_reported": N, "exact": bool, ...clock}`. `--group-by X` → fetch all (honouring `--limit`), then rows; `meta={"total": len(rows), "pages", "group_by": X, "records_counted": sum, ...}`; `truncated` carried over. Both together → UsageError.

- [ ] **Step 1: Write the failing unit tests**

Create `tests/test_aggregate.py`:

```python
from zoneinfo import ZoneInfo

import pytest

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
        recs, period="day", since="2026-10-07T00:00:00+03:00", until="2026-10-09T23:59:59+03:00", tz=tz, time_field="time"
    )
    assert [(r["period"], r["count"]) for r in rows] == [("2026-10-07", 0), ("2026-10-08", 2), ("2026-10-09", 1)]
    assert rows[1]["since"] == "2026-10-08T00:00:00+03:00"


def test_group_by_period_uses_dotted_time_field():
    tz = ZoneInfo("UTC")
    recs = [{"patrol_segments": [{"time_range": {"start_time": "2026-10-08T05:00:00Z"}}]}]
    rows = aggregate.group_by_period(
        recs, period="month", since="2026-10-01T00:00:00Z", until="2026-10-31T00:00:00Z", tz=tz,
        time_field="patrol_segments.0.time_range.start_time",
    )
    assert rows == [{"period": "2026-10", "since": "2026-10-01T00:00:00+00:00", "until": "2026-10-31T23:59:59+00:00", "count": 1}]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_aggregate.py -q`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `aggregate.py`**

```python
"""Counting and grouping records in the CLI rather than in the model's head.

The breakdown question ("how many of each priority", "events per month") has
no server-side answer in ER, so the choice is between the CLI counting and the
caller counting; a caller adding up a table is where sums go wrong.
"""

from __future__ import annotations

from . import clock
from .output import cell, pluck


def group_counts(records: list, field: str) -> tuple[list[dict], int]:
    """Count records per distinct value of `field`. Returns (rows, matched):
    `matched` is how many records carried the field at all, so a caller can
    tell "everything is None" from "there were no records"."""
    counts: dict[str, int] = {}
    matched = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        value = pluck(record, field)
        if value is not None and value != "":
            matched += 1
        key = cell(value)
        counts[key] = counts.get(key, 0) + 1
    rows = [{"group": key or "(none)", "count": n} for key, n in counts.items()]
    rows.sort(key=lambda row: (-row["count"], str(row["group"])))
    return rows, matched


def scalar_keys(records: list, limit: int = 25) -> list[str]:
    keys: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        for key, value in record.items():
            if key not in keys and not isinstance(value, (list, dict)):
                keys.append(key)
    return keys[:limit]


def group_by_period(records: list, *, period: str, since: str, until: str, tz, time_field: str) -> list[dict]:
    buckets = clock.bucket_window(since, until, period, tz)
    rows = [{"period": label, "since": lo, "until": hi, "count": 0} for label, lo, hi in buckets]
    edges = [(clock.parse_ts(lo), clock.parse_ts(hi), row) for row, (_, lo, hi) in zip(rows, buckets)]
    for record in records:
        when = clock.parse_ts(pluck(record, time_field)) if isinstance(record, dict) else None
        if when is None:
            continue
        for lo, hi, row in edges:
            if lo <= when <= hi:
                row["count"] += 1
                break
    return rows
```

- [ ] **Step 4: Run unit tests**

Run: `uv run --extra dev pytest tests/test_aggregate.py -q`
Expected: PASS.

- [ ] **Step 5: Write the failing command tests**

```python
def test_count_only_asks_the_server_once(fake):
    fake.responses["activity/events"] = {"count": 1234, "next": "x", "results": [{"id": "e1"}]}
    result = _run(["events", "search", "--count-only", "--state", "active"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["records"] == [{"count": 1234}]
    assert doc["meta"]["count_reported"] == 1234 and doc["meta"]["exact"] is True
    sent = _gets(fake)
    assert len(sent) == 1 and sent[0][2]["page_size"] == 1 and sent[0][2]["state"] == ["active"]


def test_count_only_walks_when_the_endpoint_has_no_count(fake):
    fake.responses["regions"] = [{"id": "r1"}, {"id": "r2"}]
    doc = json.loads(_run(["regions", "list", "--count_only"]).output)
    assert doc["records"] == [{"count": 2}] and doc["meta"]["exact"] is True


def test_group_by_field_and_unknown_field(fake):
    fake.responses["activity/events"] = {
        "count": 3, "next": None,
        "results": [{"priority": 300}, {"priority": 300}, {"priority": 0}],
    }
    doc = json.loads(_run(["events", "search", "--group-by", "priority"]).output)
    assert doc["records"] == [{"group": "300", "count": 2}, {"group": "0", "count": 1}]
    assert doc["meta"]["group_by"] == "priority" and doc["meta"]["records_counted"] == 3
    result = _run(["events", "search", "--group-by", "nope"])
    assert result.exit_code == 2
    assert "no record carries 'nope'" in result.output and "priority" in result.output


def test_group_by_month_needs_a_window_and_a_time_field(fake):
    fake.responses["activity/events"] = {
        "count": 2, "next": None,
        "results": [{"time": "2026-09-15T10:00:00Z"}, {"time": "2026-10-02T10:00:00Z"}],
    }
    result = _run(["events", "search", "--group-by", "month"])
    assert result.exit_code == 2 and "needs a window" in result.output
    doc = json.loads(_run(["events", "search", "--group-by", "month", "--since", "2026-09-01", "--until", "2026-10-09"]).output)
    assert [(r["period"], r["count"]) for r in doc["records"]] == [("2026-09", 1), ("2026-10", 1)]
    assert doc["meta"]["site_tz"] == "Africa/Nairobi"
    fake.responses["subjects"] = {"count": 0, "next": None, "results": []}
    result = _run(["subjects", "search", "--group-by", "day", "--today"])
    assert result.exit_code == 2 and "no timestamp to bucket" in result.output


def test_count_only_and_group_by_are_exclusive(fake):
    result = _run(["events", "search", "--count-only", "--group-by", "priority"])
    assert result.exit_code == 2 and "either --count-only or --group-by" in result.output
```

- [ ] **Step 6: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_read_commands.py -k "count_only or group_by" -q`
Expected: FAIL "No such option: --count-only".

- [ ] **Step 7: Wire into `read_commands.py`**

Add `time_field: str | None = None` to `ReadCommand`; set it on the events (`"time"`), observations (`"recorded_at"`), patrols (`"patrol_segments.0.time_range.start_time"`) rows. Options for list commands (next to `--limit`):

```python
        fn = click.option(*_option_names("group_by"), metavar="FIELD|day|week|month",
                          help="Count records per value of FIELD (dotted path), or per site-local period "
                               "(needs a window: --since/--until, --today, --last).")(fn)
        fn = click.option(*_option_names("count_only"), is_flag=True,
                          help="Report the server's count for the query in one request; no records.")(fn)
```

Callback (list kind only), replacing the single `fetch` call:

```python
        count_only, group_by = kwargs.pop("count_only", False), kwargs.pop("group_by", None)
        if count_only and group_by:
            raise click.UsageError("pass either --count-only or --group-by, not both.")
        ...
        if count_only:
            n = fetch_count(client, path, params, version=spec.version, unwrap=spec.unwrap)
            if n is None:
                records, meta = fetch(client, path, params, paginate=True, limit=limit, version=spec.version, unwrap=spec.unwrap)
                n, pages, exact = meta["total"], meta["pages"], not meta.get("truncated")
            else:
                pages, exact = 1, True
            records, meta = [{"count": n}], {"total": 1, "pages": pages, "count_reported": n, "exact": exact}
        else:
            records, meta = fetch(...)
            if group_by:
                records, meta = _grouped(ctx, client, spec, records, meta, group_by, window_meta)
```

with

```python
def _grouped(ctx, client, spec, records, meta, group_by, window_meta):
    out_meta = {k: v for k, v in meta.items() if k in ("pages", "count_reported", "truncated", "note")}
    out_meta["group_by"] = group_by
    if group_by in _clock.PERIODS:
        if not window_meta or not window_meta.get("since"):
            raise click.UsageError(f"--group-by {group_by} needs a window to divide: pass --since/--until, --today, or --last.")
        if not spec.time_field:
            raise click.UsageError(f"{spec.group} records have no timestamp to bucket by {group_by}; group by a field instead.")
        info = get_clock(ctx, client, required=True)
        tz = _clock.site_tz(info)
        if tz is None:
            raise click.ClickException("the site reported no usable timezone, so --group-by cannot say where a day begins.")
        until = window_meta.get("until") or info.get("local") or info["utc"]
        rows = aggregate.group_by_period(records, period=group_by, since=window_meta["since"], until=until, tz=tz, time_field=spec.time_field)
    else:
        rows, matched = aggregate.group_counts(records, group_by)
        if records and not matched:
            keys = ", ".join(aggregate.scalar_keys(records))
            raise click.UsageError(f"no record carries {group_by!r}; fields seen: {keys}")
    out_meta["total"] = len(rows)
    out_meta["records_counted"] = sum(r["count"] for r in rows)
    return rows, out_meta
```

Keep `meta["window"]` and the clock enrichment after this branch so grouped output carries them too.

- [ ] **Step 8: Run the full suite and linters**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check src tests && uv run --extra dev ruff format --check src tests`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/earthranger_cli/aggregate.py src/earthranger_cli/read_commands.py tests/test_aggregate.py tests/test_read_commands.py
git commit -m "Read commands: --count-only and --group-by FIELD|day|week|month

--count-only reads DRF's count beside a one-record page, so a total is
one request instead of a walk; endpoints with no count are walked and
reported as exact or a floor. --group-by counts per field value, or
per site-local day/week/month over the command's window, using each
resource's own timestamp. Both exist because a caller summing a table
is where the numbers went wrong.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: `events export` and `observations export` (server CSV, 403 → records)

**Files:**
- Modify: `src/earthranger_cli/read_commands.py` (`kind="raw"`, `ReadCommand.fallback`, two rows, raw branch in callback)
- Test: `tests/test_read_commands.py`

**Interfaces:**
- `ReadCommand.kind` accepts `"raw"`: no `--limit`, `--page-size`, `--fields`, `--format`, `--count-only`, `--group-by`; `-o PATH` writes the body verbatim; stdout gets the body verbatim otherwise; the `Done.` line (stderr) reads `Done. N data row(s) written to PATH (text/csv).` where N is newline count minus the header.
- `ReadCommand.fallback: dict | None` — `{"path": str, "keep": {export_param: records_param}, "drop": set, "add": dict, "window": kind}`. On `ERClientPermissionDenied` the callback maps the sent params through it; any param not in `keep` or `drop` → re-raise the 403 (Review Focus 5); otherwise a stderr `note:` and the records document (JSON) is emitted instead, through the normal list path (paginated, `--fields` not available, so JSON only).
- Before writing the rows, confirm das's parameter names: `grep -n "query_params\|GET.get\|request.GET" ~/padas/das/das/activity/views.py | grep -i export -A3` and the same for `trackingdata/export` in `~/padas/das/das/observations/views.py`. Adjust flag names below to what das actually reads; keep otus's spellings as the documented ones where they match.

- [ ] **Step 1: Write the failing tests**

```python
def test_events_export_writes_the_servers_csv(fake, tmp_path):
    from conftest import FakeResponse

    fake.responses["activity/events/export"] = FakeResponse(
        None, content_type="text/csv", text_override="id,Species\ne1,Elephant\ne2,Lion\n"
    )
    target = tmp_path / "events.csv"
    result = _run(["events", "export", "--today", "--event-type", "carcass", "-o", str(target)])
    assert result.exit_code == 0, result.output
    assert target.read_text() == "id,Species\ne1,Elephant\ne2,Lion\n"
    assert result.stdout == ""
    assert result.stderr.strip() == f"Done. 2 data row(s) written to {target} (text/csv)."
    sent = [c for c in fake.calls if c[0] == "_get_response" and c[1] == "activity/events/export"][0][2]
    f = json.loads(sent["filter"])
    assert f["date_range"]["lower"] == "2026-10-09T00:00:00+03:00"
    assert f["event_type"] == ["t-carcass"]  # resolved id folded into filter


def test_events_export_to_stdout(fake):
    from conftest import FakeResponse

    fake.responses["activity/events/export"] = FakeResponse(None, content_type="text/csv", text_override="id\n")
    result = _run(["events", "export"])
    assert result.output == "id\n"


def test_export_403_falls_back_to_records(fake):
    from erclient.er_errors import ERClientPermissionDenied

    def denied(path, **kwargs):
        raise ERClientPermissionDenied("no export permission")

    fake.responses["activity/events/export"] = denied
    fake.responses["activity/events"] = {"count": 1, "next": None, "results": [{"id": "e1"}]}
    result = _run(["events", "export", "--since", "2026-10-01"])
    assert result.exit_code == 0, result.output
    assert "note: this account may not export" in result.stderr
    doc = json.loads(result.stdout)
    assert doc["records"] == [{"id": "e1"}]
    sent = _gets(fake)[0][2]
    assert sent["include_details"] == "true" and json.loads(sent["filter"])["date_range"]["lower"] == "2026-10-01"


def test_export_403_with_an_untranslatable_flag_stays_a_403(fake):
    from erclient.er_errors import ERClientPermissionDenied

    def denied(path, **kwargs):
        raise ERClientPermissionDenied("no export permission")

    fake.responses["trackingdata/export"] = denied
    result = _run(["observations", "export", "--subject-id", "s1", "--current-status"])  # Review Focus 5
    assert result.exit_code == 1
    assert "no export permission" in result.output
    assert _gets(fake) == []
```

For the `denied` callables, extend `FakeER._get`'s `return_response` branch: `if callable(body): return body(path, params=params)`. For the event-type resolution in the first test, seed `fake.event_types = [{"id": "t-carcass", "value": "carcass", "display": "Carcass"}]` in the test before running.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_read_commands.py -k export -q`
Expected: FAIL "No such command 'export'".

- [ ] **Step 3: Implement**

Rows (after `events get` and after `observations search`):

```python
    ReadCommand(
        "events", "export", "activity/events/export",
        "Export events as the server's own CSV: columns and values are the site's display "
        "names (what the form shows), which the JSON records do not carry.",
        "raw",
        flags=(
            Flag("filter", "ER events JSON filter; --since/--until merge into it."),
            Flag("event_type", "Event type value(s), display name(s) or id(s), comma-separated; resolved to ids.", "list"),
            Flag("state", "new | active | resolved."),
            Flag("bbox", "Bounding box: west,south,east,north."),
            Flag("value_cols", "Also write each field's internal value column.", "bool"),
            Flag("display_cols", "Write fields under their display names (server default).", "bool"),
        ),
        window="filter",
        resolve=_resolve_export_event_types,
        fallback={"path": "activity/events", "keep": {"filter": "filter", "state": "state", "bbox": "bbox"},
                  "drop": {"value_cols", "display_cols"}, "add": {"include_details": "true"}},
    ),
    ReadCommand(
        "observations", "export", "trackingdata/export",
        "Export raw observations as the server's CSV (ER's own column names).",
        "raw",
        flags=(
            Flag("subject_id", "Subject id."),
            Flag("source_provider", "Source provider key."),
            Flag("filter", "ER observation filter."),
            _INCLUDE_INACTIVE,
            Flag("current_status", "Only currently assigned sources.", "bool"),
            Flag("subject_chronofile", "Subject chronofile."),
            Flag("record_serial_base", "Serial base for record numbering.", "int"),
            Flag("max_records", "Stop after this many records.", "int"),
        ),
        window="after_before",
        fallback={"path": "observations", "keep": {"after_date": "since", "before_date": "until", "subject_id": "subject_id"},
                  "drop": set(), "add": {"include_details": "true"}},
    ),
```

`_resolve_export_event_types(client, params)`: call `_resolve_event_types` to get ids, then move them from `params["event_type"]` into the `filter` JSON as `{"event_type": [ids]}` (merge like `apply_window` does), because the export view reads only `filter`. Verify against das first (Interfaces note); if the export view also reads a bare `event_type` param, send that instead and say so in a comment.

Callback: build params as today; for `kind == "raw"`:

```python
        if spec.kind == "raw":
            try:
                body, content_type = fetch_text(client, path, params)
            except ERClientPermissionDenied as e:
                plan = spec.fallback
                mapped = dict(plan["add"]) if plan else None
                if plan:
                    for name, value in params.items():
                        if name in plan["drop"]:
                            continue
                        if name not in plan["keep"]:
                            mapped = None
                            break
                        mapped[plan["keep"][name]] = value
                if mapped is None:
                    raise
                click.echo(
                    f"note: this account may not export ({e}); returning the matching records as JSON "
                    f"from GET /api/v1.0/{plan['path']} instead.", err=True,
                )
                records, meta = fetch(client, plan["path"], mapped, paginate=True)
                emit(records, meta, output)
                return
            _emit_text(body, content_type, output)
            return
```

with

```python
def _emit_text(body: str, content_type: str, output: str | None) -> None:
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        rows = max(body.count("\n") - 1, 0) if body.strip() else 0
        click.echo(f"Done. {rows} data row(s) written to {output} ({content_type or 'text/csv'}).", err=True)
    else:
        click.echo(body, nl=False)
```

Option wiring: `limit`, `page_size`, `output_options`, `count_only`/`group_by` are added only when `spec.kind == "list"`; `-o` and `window_options` for raw too. Update `test_every_command_is_registered_with_output_option`: `("fields" in names) == (spec.kind != "raw")`.

Note on `fetch_text` through `FakeER`: it goes through the `return_response` branch; add the `callable(body)` hook described in Step 1 to `conftest.py`.

- [ ] **Step 4: Run the suite and linters**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check src tests && uv run --extra dev ruff format --check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/earthranger_cli/read_commands.py tests/test_read_commands.py tests/conftest.py
git commit -m "events export, observations export: the server's CSV, records on 403

The two exports are the only reads ER answers in CSV, and the events
one is built from the site's schema (display names and labels the JSON
records never carry), so the body is written exactly as sent. Export
is a per-user permission: on 403 the command returns the matching
records as JSON and says so, unless a flag has no records equivalent,
in which case the 403 stands rather than widening the question.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: `--where KEY=VALUE` on `events search`

**Files:**
- Modify: `src/earthranger_cli/aggregate.py` (`parse_where`, `details_match`, `filter_details`)
- Modify: `src/earthranger_cli/read_commands.py` (`Flag` kind `"multi"`; `--where` on `events search` and `events export`'s fallback path is out of scope; callback branch)
- Test: `tests/test_aggregate.py`, `tests/test_read_commands.py`

**Interfaces:**
```python
def parse_where(items: tuple[str, ...] | None) -> list[tuple[str, str]]   # UsageError on malformed
def details_match(record: dict, key: str, value: str) -> bool | None      # None when the detail is absent
def filter_details(records: list, where: list[tuple[str, str]]) -> tuple[list, str]   # (kept, note)
```
Matching: case-insensitive string equality against `event_details[key]`; a dict value matches on its `name`, `value` or `display`; a list matches if any element matches. `--where` forces `include_details=true`. After the fetch (and before `--group-by`/`--count-only` counting), records are filtered; `meta.fetched` is the pre-filter count, `meta.total` the kept count, `meta.where` the pairs, `meta.note` the filter note. With `--count-only`, the command walks all pages (no server count) and counts the kept records.

- [ ] **Step 1: Write the failing unit tests**

```python
def test_parse_where_and_details_match():
    assert aggregate.parse_where(("species=Buffalo", " cause = poached ")) == [("species", "Buffalo"), ("cause", "poached")]
    with pytest.raises(click.UsageError, match="KEY=VALUE"):
        aggregate.parse_where(("species",))
    rec = {"event_details": {"species": "buffalo", "tags": ["a", "B"], "who": {"name": "Ann", "value": "ann"}}}
    assert aggregate.details_match(rec, "species", "BUFFALO") is True
    assert aggregate.details_match(rec, "tags", "b") is True
    assert aggregate.details_match(rec, "who", "ann") is True
    assert aggregate.details_match(rec, "species", "lion") is False
    assert aggregate.details_match(rec, "cause", "x") is None
    assert aggregate.details_match({"event_details": None}, "species", "x") is None


def test_filter_details_keeps_matches_and_names_silent_types():
    recs = [
        {"event_type": "carcass", "event_details": {"species": "buffalo"}},
        {"event_type": "carcass", "event_details": {"species": "lion"}},
        {"event_type": "elephant_carcass", "event_details": {}},
    ]
    kept, note = aggregate.filter_details(recs, [("species", "buffalo")])
    assert [r["event_details"]["species"] for r in kept] == ["buffalo"]
    assert "1 of 3 event(s) matched" in note
    assert "1 event(s) have no 'species' detail" in note and "elephant_carcass (1)" in note
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_aggregate.py -q`
Expected: AttributeError `parse_where`.

- [ ] **Step 3: Implement in `aggregate.py`**

```python
import click


def parse_where(items) -> list[tuple[str, str]]:
    pairs = []
    for item in items or ():
        key, sep, value = str(item).partition("=")
        if not sep or not key.strip() or not value.strip():
            raise click.UsageError(f"--where takes KEY=VALUE, e.g. --where species=buffalo; got {item!r}.")
        pairs.append((key.strip(), value.strip()))
    return pairs


def _matches(value, wanted: str) -> bool:
    if isinstance(value, dict):
        return any(_matches(value.get(k), wanted) for k in ("name", "value", "display"))
    if isinstance(value, list):
        return any(_matches(v, wanted) for v in value)
    if value is None:
        return False
    return str(value).casefold() == wanted.casefold()


def details_match(record: dict, key: str, value: str) -> bool | None:
    details = record.get("event_details") if isinstance(record, dict) else None
    if not isinstance(details, dict) or key not in details:
        return None
    return _matches(details[key], value)


def filter_details(records: list, where: list[tuple[str, str]]) -> tuple[list, str]:
    """Records whose event_details match every KEY=VALUE, and a note that names
    the event types carrying no such detail at all (a type can say "elephant"
    in its name instead of in a species field; those cannot match and should
    be counted by type, not as none)."""
    kept = []
    silent: dict[str, dict[str, int]] = {k: {} for k, _ in where}
    for r in records:
        ok = True
        for key, value in where:
            m = details_match(r, key, value)
            if m is None:
                t = str(r.get("event_type") or "?") if isinstance(r, dict) else "?"
                silent[key][t] = silent[key].get(t, 0) + 1
            if not m:
                ok = False
        if ok:
            kept.append(r)
    spec = ", ".join(f"{k}={v}" for k, v in where)
    parts = [f"--where {spec} was applied here, to event_details: {len(kept)} of {len(records)} event(s) matched."]
    for key, types in silent.items():
        if types:
            named = ", ".join(f"{t} ({n})" for t, n in sorted(types.items(), key=lambda kv: -kv[1])[:6])
            parts.append(
                f"{sum(types.values())} event(s) have no {key!r} detail at all ({named}); "
                "they cannot match it — count those by event type instead."
            )
    return kept, " ".join(parts)
```

- [ ] **Step 4: Write the failing command test**

```python
def test_where_filters_details_client_side_and_counts_the_matches(fake):
    fake.responses["activity/events"] = {
        "count": 3, "next": None,
        "results": [
            {"id": "e1", "event_type": "carcass", "event_details": {"species": "buffalo"}},
            {"id": "e2", "event_type": "carcass", "event_details": {"species": "lion"}},
            {"id": "e3", "event_type": "elephant_carcass", "event_details": {}},
        ],
    }
    result = _run(["events", "search", "--where", "species=Buffalo"])
    assert result.exit_code == 0, result.output
    assert _gets(fake)[0][2]["include_details"] == "true"
    doc = json.loads(result.output)
    assert [r["id"] for r in doc["records"]] == ["e1"]
    assert doc["meta"]["total"] == 1 and doc["meta"]["fetched"] == 3
    assert doc["meta"]["where"] == {"species": "Buffalo"}
    assert "1 of 3 event(s) matched" in doc["meta"]["note"]

    doc = json.loads(_run(["events", "search", "--where", "species=buffalo", "--count-only"]).output)
    assert doc["records"] == [{"count": 1}]
    assert _gets(fake)[1][2]["page_size"] == 100  # walked, not the one-record count

    result = _run(["events", "search", "--where", "species"])
    assert result.exit_code == 2 and "KEY=VALUE" in result.output
```

- [ ] **Step 5: Run to verify it fails, then wire**

Run: `uv run --extra dev pytest tests/test_read_commands.py -k where -q` → FAIL "No such option: --where".

In `read_commands.py`: `Flag` kind `"multi"` → `click.option(*names, multiple=True, metavar="KEY=VALUE", help=...)` and the params loop skips `"multi"` flags (they are not query params). Add `Flag("where", "Keep events whose event_details say KEY is VALUE; repeatable. Applied by the CLI after fetching; with --count-only or --group-by the CLI counts the matches.", "multi")` to `events search`. In the callback, before building params:

```python
        where = aggregate.parse_where(kwargs.get("where")) if any(f.kind == "multi" for f in spec.flags) else []
        if where:
            params["include_details"] = "true"
```

In the fetch branch: if `where`, never take the `fetch_count` shortcut — fetch with pagination, then `records, note = aggregate.filter_details(records, where)`; set `meta["fetched"] = meta["total"]`, `meta["total"] = len(records)`, `meta["where"] = dict(where)`, `meta["note"] = note` (join with any truncation note); then apply `count_only` (`[{"count": len(records)}]`, `exact = not truncated`) or `group_by` on the kept records.

- [ ] **Step 6: Run the suite and linters**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check src tests && uv run --extra dev ruff format --check src tests`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/earthranger_cli/aggregate.py src/earthranger_cli/read_commands.py tests/test_aggregate.py tests/test_read_commands.py
git commit -m "events search: --where KEY=VALUE filters event_details in the CLI

ER has no server-side filter on event details, so the CLI applies it
after fetching (forcing include_details) and counts the matches for
--count-only and --group-by itself. The note names the event types
that carry no such detail at all, since a type can say its species in
its name instead of a field.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Glob patterns, nearest-match suggestions, subject-group names

**Files:**
- Modify: `src/earthranger_cli/read_commands.py` (`_resolve_event_types`; new `_resolve_subject_group`; `subjects search` row gets `resolve=`)
- Test: `tests/test_read_commands.py`

**Interfaces:**
- `_resolve_event_types`: a wanted value containing any of `*?[` is a pattern (`fnmatch.fnmatchcase`, case-folded) matched against every type's `value` and `display`; all hits' ids are added and a stderr `note:` lists them; no hit → UsageError with the server's values. An exact miss adds `Did you mean: a, b?` from `difflib.get_close_matches(w, values + displays, n=3, cutoff=0.6)`.
- `_resolve_subject_group(client, params)`: a non-UUID `subject_group` is looked up in `GET subjectgroups?flat=true&include_inactive=true` (paginated via `fetch`) by `name` (case-folded exact); one hit → id; several → UsageError listing `name (id)`; none → UsageError with close matches.

- [ ] **Step 1: Write the failing tests**

```python
def test_event_type_glob_matches_values_and_displays(fake):
    fake.event_types = [
        {"id": "t1", "value": "carcass_rep", "display": "Carcass Report"},
        {"id": "t2", "value": "elephant_carcass", "display": "Carcass - Elephant"},
        {"id": "t3", "value": "sighting", "display": "Sighting"},
    ]
    fake.responses["activity/events"] = {"count": 0, "next": None, "results": []}
    result = _run(["events", "search", "--event-type", "*carcass*"])
    assert result.exit_code == 0, result.output
    assert sorted(_gets(fake)[0][2]["event_type"]) == ["t1", "t2"]
    assert "note: --event-type '*carcass*' matched 2 type(s): carcass_rep, elephant_carcass" in result.stderr
    result = _run(["events", "search", "--event-type", "zebra*"])
    assert result.exit_code == 2 and "matches no event type" in result.output


def test_event_type_miss_suggests_close_matches(fake):
    fake.event_types = [{"id": "t1", "value": "geofence_break", "display": "Geofence Break"}]
    result = _run(["events", "search", "--event-type", "geofence_brake"])
    assert result.exit_code == 2
    assert "Did you mean: geofence_break" in result.output


def test_events_post_event_type_is_never_resolved(fake):
    # guardrail: posting takes the exact value; no pattern, no lookup
    fake.event_types = [{"id": "t1", "value": "carcass_rep", "display": "Carcass Report"}]
    result = _run(["events", "post", "--event-type", "carcass*", "--no-validate"])
    posted = [c for c in fake.calls if c[0] == "post_event"]
    assert posted and posted[0][1]["event_type"] == "carcass*"


def test_subject_group_name_is_resolved_to_an_id(fake):
    fake.responses["subjectgroups"] = {
        "count": 2, "next": None,
        "results": [{"id": "g1", "name": "Rangers"}, {"id": "g2", "name": "Elephants"}],
    }
    fake.responses["subjects"] = {"count": 0, "next": None, "results": []}
    result = _run(["subjects", "search", "--subject-group", "elephants"])
    assert result.exit_code == 0, result.output
    assert _gets(fake)[0][1:3] == ("subjectgroups", {"flat": "true", "include_inactive": "true", "page_size": 100})
    assert _gets(fake)[1][2]["subject_group"] == "g2"
    result = _run(["subjects", "search", "--subject-group", "elefants"])
    assert result.exit_code == 2 and "Did you mean: Elephants" in result.output
    fake.calls.clear()
    _run(["subjects", "search", "--subject-group", "0b1a7c2e-1111-4222-8333-444455556666"])
    assert _gets(fake)[0][1] == "subjects"  # a UUID needs no lookup
```

(Check how `tests/test_cli.py` currently posts events with `FakeER` — `--no-validate` and any required flags like `--server` — and copy that invocation shape into the guardrail test.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --extra dev pytest tests/test_read_commands.py -k "glob or close_matches or subject_group_name or never_resolved" -q`
Expected: the glob/suggest/subject-group tests FAIL; the post guardrail passes already (keep it as the pin).

- [ ] **Step 3: Implement**

In `_resolve_event_types`, after `by_display` is built and before the loop:

```python
    import fnmatch, difflib  # (module-level imports)
    ...
    for w in wanted:
        if _UUID_RE.match(w): ...
        if any(ch in w for ch in "*?["):
            pat = w.casefold()
            hits = [t for t in types if t.get("id") and (
                fnmatch.fnmatchcase(str(t.get("value") or "").casefold(), pat)
                or fnmatch.fnmatchcase(str(t.get("display") or "").casefold(), pat))]
            if not hits:
                raise click.UsageError(f"--event-type {w!r} matches no event type on this server; values: {', '.join(sorted(by_value))}")
            resolved.extend(t["id"] for t in hits)
            click.echo(f"note: --event-type {w!r} matched {len(hits)} type(s): {', '.join(sorted(t['value'] for t in hits))}.", err=True)
            continue
        ...
        close = difflib.get_close_matches(w, list(by_value) + [t.get("display") for t in types if t.get("display")], n=3, cutoff=0.6)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise click.UsageError(f"unknown event type {w!r}.{hint} Pass a value, display name or id; values on this server: {available}")
```

Keep the message prefix `unknown event type` so the existing test on it still passes (check with `grep -n "unknown event type" tests/`).

```python
def _resolve_subject_group(client, params: dict) -> dict:
    wanted = params.get("subject_group")
    if not wanted or _UUID_RE.match(wanted):
        return params
    groups, _ = fetch(client, "subjectgroups", {"flat": "true", "include_inactive": "true"}, paginate=True)
    hits = [g for g in groups if str(g.get("name") or "").casefold() == wanted.casefold() and g.get("id")]
    if len(hits) == 1:
        params["subject_group"] = hits[0]["id"]
        return params
    if hits:
        options = ", ".join(sorted(f"{g['name']} ({g['id']})" for g in hits))
        raise click.UsageError(f"subject group {wanted!r} matches {len(hits)} groups: {options}. Pass the id.")
    names = [str(g.get("name")) for g in groups if g.get("name")]
    close = difflib.get_close_matches(wanted, names, n=3, cutoff=0.6)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise click.UsageError(f"unknown subject group {wanted!r}.{hint} Groups on this server: {', '.join(sorted(names))}")
```

Set `resolve=_resolve_subject_group` on the `subjects search` row and change its `subject_group` flag help to "Subject group name or id."

- [ ] **Step 4: Run the suite and linters**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check src tests && uv run --extra dev ruff format --check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/earthranger_cli/read_commands.py tests/test_read_commands.py
git commit -m "Name resolution: glob patterns, nearest-match hints, subject-group names

--event-type '*carcass*' selects every type whose value or display
matches and says which on stderr; a miss suggests the closest values.
subjects search --subject-group takes a group name as well as an id.
events post, apply and pull are untouched: they pass exact values.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: README examples parse against the real command tree

**Files:**
- Create: `tests/test_readme_examples.py`

**Interfaces:** none produced; consumes `earthranger_cli.cli.main`.

- [ ] **Step 1: Write the test, with a deliberately bad example pinned first**

```python
"""Every `er ...` line in a README code block must parse against the real click
tree: options exist, subcommands exist, nothing is left over. This is what
would have caught the stale `profile use` wrapper docs (PR 34)."""

import re
import shlex
from pathlib import Path

import click
import pytest

from earthranger_cli.cli import main

README = Path(__file__).resolve().parents[1] / "README.md"
_FENCE = re.compile(r"```(?:bash|sh|zsh|console)?\n(.*?)```", re.S)


def example_lines() -> list[str]:
    lines = []
    for block in _FENCE.findall(README.read_text()):
        buf = ""
        for raw in block.splitlines():
            line = raw.strip()
            if buf:
                line = buf + " " + line
                buf = ""
            if line.endswith("\\"):
                buf = line[:-1].rstrip()
                continue
            if line.startswith("er "):
                lines.append(line)
    return lines


def parse(args: list[str]) -> None:
    """Resolve groups and parse options without running anything."""
    ctx = main.make_context("er", list(args), resilient_parsing=True)
    cmd: click.Command = main
    while isinstance(cmd, click.Group):
        rest = list(ctx.protected_args) + list(ctx.args) if hasattr(ctx, "protected_args") else list(ctx.args)
        if not rest:
            return  # `er --help`-style: a group with nothing after it
        name, sub, rest = cmd.resolve_command(ctx, rest)
        if sub is None:
            raise AssertionError(f"no such command {rest[0]!r}")
        ctx = sub.make_context(name, rest, parent=ctx, resilient_parsing=True)
        cmd = sub
    assert not ctx.args, f"unparsed arguments: {ctx.args}"


def test_parse_rejects_a_bad_example():
    with pytest.raises((click.NoSuchOption, click.UsageError)):
        parse(["subjects", "search", "--bogus"])
    with pytest.raises(AssertionError):
        parse(["subjects", "frobnicate"])


@pytest.mark.parametrize("line", example_lines(), ids=lambda s: s[:60])
def test_readme_example_parses(line):
    args = shlex.split(line, comments=True)[1:]  # drop the leading `er`
    parse(args)
```

- [ ] **Step 2: Run it**

Run: `uv run --extra dev pytest tests/test_readme_examples.py -q`
Expected: `test_parse_rejects_a_bad_example` PASSES (it pins the helper's teeth); every README line passes too. If a README line fails, the README is wrong — fix the README (Task 11 adds the new examples, so re-run there). If the helper raises on `ctx.protected_args` (click 8.2+ removed it), use `ctx.args` only.

- [ ] **Step 3: Commit**

```bash
git add tests/test_readme_examples.py
git commit -m "tests: every README er example must parse against the real command tree

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Documentation — README, spec status

**Files:**
- Modify: `README.md` (Commands table; Reading data section; agent-skills examples)
- Modify: `docs/superpowers/specs/2026-09-02-er-cli-parity-design.md` (§P3 checkboxes with dates)
- Modify: `docs/superpowers/plans/2026-10-09-otus-parity.md` (this file: tick the boxes)

- [ ] **Step 1: README Commands table** — add rows:

```
| `now` | Server UTC time, site local time and timezone, and today's `--since/--until` bounds in site time |
| `events export [--since/--until/--today/--last] [--event-type ...] [-o F.csv]`, `observations export --subject-id ID [...]` | The server's own CSV (events: the site's display names); on 403 the matching records as JSON, with a note |
```

and in the `events search` row append: `; --since/--until/--today/--yesterday/--last 7d (site time); --where species=buffalo filters event_details`. Add a line after the table: "Every read command takes `--fields a,b.c` and `--format json|tsv|csv`; every paginated one takes `--count-only` and `--group-by FIELD|day|week|month`."

- [ ] **Step 2: README "Reading data" section** — after the `meta` bullet list add a sub-section:

```markdown
### Windows, counts, columns

- **Site time.** `--today`, `--yesterday` and `--last 7d` (also `30m`, `50h`,
  `2w`) are computed from the site's clock and timezone, not the caller's;
  `er now` prints both and today's bounds. A bare `--until 2026-08-31` means
  the end of that day. Every document's `meta` carries `server_utc`,
  `site_now` and `site_tz`; a window flag adds `meta.window`.
- **Counts.** `--count-only` asks the server for its total in one request
  (`records: [{"count": N}]`, `meta.exact`); `--group-by priority` counts per
  value, `--group-by month` per site-local period over the window.
- **Columns.** `--fields id,title,reported_by.name` keeps only those dotted
  paths (`coordinates.1` is latitude); `--format tsv` or `csv` writes a table
  of them instead of JSON.
- **Details.** `events search --where species=buffalo` keeps events whose
  `event_details` say so (applied by the CLI, which then does the counting).
- **Exports.** `events export` and `observations export` return the server's
  CSV unchanged; if the account may not export, the matching records come
  back as JSON with a note.
```

And three examples in "How agent skills use it":

```bash
# Today's events in site time, three columns, as TSV
er events search --today --fields id,time,event_type --format tsv -o /tmp/today.tsv
# How many active patrols — one request, no records
er patrols search --status active --count-only
# Carcasses per month this quarter
er events search --event-type '*carcass*' --since 2026-07-01 --until 2026-09-30 --group-by month
```

(Spell `patrols search --state` with whatever the row's flag is; Task 7's das check decides `--status` vs `--state`.)

- [ ] **Step 3: Run the README examples test and the suite**

Run: `uv run --extra dev pytest -q`
Expected: PASS, including every new README line.

- [ ] **Step 4: Spec** — tick each §P3 item `[x]` with `(2026-10-09, this PR)`; leave the "Maybe" line unticked.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/superpowers/specs/2026-09-02-er-cli-parity-design.md docs/superpowers/plans/2026-10-09-otus-parity.md
git commit -m "README and spec: document windows, counts, columns, exports, --where

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** P3 items 1–8 map to Tasks 1, 5+3+4, 6, 7, 8, 2, 9, 10; the "Maybe" tier (`link`, `analyzers`, `--with-distance`) is deliberately not planned. Guardrails are pinned by tests in Tasks 1 (human output unchanged), 9 (post never resolves), 10 (parse-only).
- **Type consistency:** `emit(records, meta, output, *, fields, fmt)` (Task 1) is what Tasks 4, 6, 7, 8 call; `follow_pages` returns a 4-tuple from Task 2 on and `_all_event_types` is updated there; `get_clock(ctx, client, *, required)` is defined in Task 4 and used in 5 and 6; `WindowRequest`/`resolve_window` signatures in Task 5 match their callers; `ReadCommand` fields are added in the task that first uses them (`window`, `default_window` in 5; `time_field` in 6; `fallback` in 7).
- **Review Focus pins:** 1 → `test_inclusive_until_extends_a_bare_date_only` and `test_patrols_last_and_bare_until`; 2 → `test_last_bounds_subtracts_in_utc_across_dst`; 3 → `test_site_tz_prefers_iana_name_then_offset_then_none`, `test_today_without_site_timezone_exits_1`, `test_meta_clock_omits_site_fields_without_a_usable_timezone`; 4 → `test_pluck_follows_dotted_paths_and_list_indexes`; 5 → `test_export_403_with_an_untranslatable_flag_stays_a_403`.
