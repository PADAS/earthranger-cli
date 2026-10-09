# er-cli parity — absorbing the agent-facing read surface

Date: 2026-09-02
Status: P0 implemented 2026-09-23; P1 done 2026-10-07; P2 closed 2026-10-08.
P3 (parity with the otus er-cli, which superseded tusker's) proposed and
implemented 2026-10-09 (`feat/otus-parity`); only its "Maybe" tier is open —
see §Comparison with otus er-cli and §P3.

## Overview

`earthranger-cli` (console script `er`) is an **authoring tool**: it
creates and edits EarthRanger event categories, choices, and v2 event
types from a YAML DSL, and posts events. That remains its identity.

`~/padas/tusker/packages/er-cli` is a second, independent CLI that also
installs as `er`. It is an **agent-facing read wrapper** over the ER REST
API: a curated registry of `er <resource> <action>` commands whose flags
are derived from a bundled OpenAPI spec, with auto-pagination and a fixed
JSON output shape that agent skills can write to a file and read back.
It is read-only, bearer-token only, and (as of this writing) not consumed
by any tusker skill — only its own README and tests reference it.

Two `er` binaries cannot coexist in one environment. This spec proposes
that this repo absorb er-cli's key features so tusker can depend on it
and `packages/er-cli` can be retired. The authoring surface is unchanged;
everything below is additive.

### Goals

- A deterministic, agent-friendly output contract (`{records, meta}` JSON,
  `-o PATH`) on every read command.
- A bearer-token auth path so sandboxes with an injected token never need
  an interactive login.
- The read-only resource commands er-cli exposes (subjects, tracks,
  observations, events, patrols, sources, subject groups, spatial
  feature groups and features, featuresets, regions, status, whoami),
  with auto-pagination and
  `--limit`.
- Retries with backoff on transient failures.

### Non-goals

- Exposing POST/PATCH generically from the OpenAPI spec. Writes stay
  curated (apply, post) — this is what keeps the tool an authoring tool.
- Reimplementing token refresh à la er-cli's TODO; this repo already has
  it.
- Changing the DSL, `apply`, or `pull` in any way.

This revises the 2026-08-31 design's non-goal "managing subjects,
patrols, users, or any non-event objects": *reading* them is now in
scope; *writing* them is still out.

## Comparison (as of 2026-09-02)

| Area | earthranger-cli (this repo) | er-cli (tusker) |
|---|---|---|
| Purpose | Author categories / types / choices via YAML DSL (`apply`, `pull`); post events | Agent-facing wrapper over ER read endpoints; deterministic JSON output |
| Command tree | Hand-written click commands | Generated: `resources.py` registry maps `<resource> <action>` → OpenAPI `operationId`; flags derived at runtime from the bundled 14k-line `openapi.yaml` (one `--flag` per query param, one positional per path param) |
| Auth | Username/password → OAuth session cached per profile, auto-refresh, file locking, persisted default profile (`er profile use`), overridable per command/shell by `--profile` / `ER_PROFILE` (zsh wrapper retired in #33) | Bearer token only: `--token` / `ER_TOKEN` / profile in `~/.earthranger/config.json` with `default_profile`; no refresh |
| Server | Site name shorthand (`myreserve` → `https://myreserve.pamdas.org`) or URL | Full URL (adds `https://` if missing) |
| Output | Human text (aligned columns); JSON only on `show event-type` | Always `{"records": [...], "meta": {total, pages, count_reported?}}`; `-o/--output PATH` writes file + one-line stderr summary; stdout otherwise |
| Pagination | Only for choices (`_collect_pages`) | Every `list` command follows `next`; `--limit N` caps; `page_size=100` default |
| Retries | erclient defaults (`_get` retries 5× with fixed 5 s sleeps, 502 only at the adapter; `_call` for writes has none); choices pass `max_retries=0` | Exponential backoff on 429/5xx and network errors, 3 attempts |
| Convenience | `--field k=v`, `--location LAT,LON`, batch post from YAML | `events search --event_type` accepts type *values* or display names and resolves to UUIDs; unknown name errors with the sorted list of valid values |
| Reads | `events list categories`, `events list event-types`, `events show event-type`, `choices list/show`, `auth status` (local session only) | `status show`, `auth whoami`, `subjects search/get`, `tracks get` (v2), `observations search`, `events search/get`, `event-types list`, `event-categories list`, `patrols search/get`, `fences list`, `featuresets get`, `regions list`, `sources search/get`, `subject-groups list/get`, `subject-sources search` |
| Writes | apply (categories, types, choices), events post | None (deliberately) |
| Stack | click, earthranger-client (requests) | argparse, httpx, PyYAML — 589 lines |
| Tests | 11 modules, broad | 8 offline tests: spec load, flag derivation, pagination, name resolution, missing creds |

Everything this repo has that er-cli lacks (password login + refresh,
profile locking, DSL apply/pull, posting, choices) stays as-is.

## Comparison with otus er-cli (as of 2026-10-09)

Tusker's `packages/er-cli` (one commit, 2026-07-22) was replaced by
`~/padas/otus/packages/er-cli`: same package name and `er` entry point,
same registry-over-OpenAPI design, but 29 commits through 2026-09-25,
~4k lines of implementation (vs ~270), 366 offline tests (vs 8), and
about 20 `SKILL.md` files in `otus/agents/otus/skills/` that call it.
Its extras were driven by evaluation runs of those agents, so they are
the best available signal of what agents actually need from an ER CLI.
This section records what it adds over the table above and whether this
repo should follow.

**Unchanged from tusker, and already at parity here:** the resource set
(subjects, tracks, observations, events, patrols, sources, subject
groups, subject-sources, spatial feature groups and features, regions,
status, whoami), the `{records, meta}` envelope, `-o PATH`, following
`next` with `--limit` and `page_size=100`, three retries with backoff on
429/5xx/network, exit codes 0/1/2, event-type name → id resolution.

**Auth:** still bearer-token only (`--token`/`ER_TOKEN`, or
`~/.earthranger/config.json` with `default_profile`), no login, no
refresh. Our auth is a strict superset; nothing to take.

**What otus adds, and the call for this repo:**

| Otus capability | Here? | Adopt? | Notes |
|---|---|---|---|
| `--fields a,b.c,list.1` projection; `--format json\|tsv\|csv` (tsv/csv require `--fields`) | No | **Yes — highest value** | Shrinks what an agent reads back; the main reason their skills shell out instead of calling the API. Dotted paths, list indexes. |
| Site-timezone windows: `--today`, `--yesterday`, `--last 7d`, `er now`; `meta.server_utc` / `site_now` / `site_tz` on every response (one extra `/status/` call) | No (`--since` defaults to 24 h on observations only) | **Yes**, but the clock only on demand | Agents get the site's "today" wrong without it. Applied to events, patrols, observations, tracks, exports. The `meta` time fields appear only when a window flag or period grouping fetched the clock (see §P3). |
| `--count_only` (one `page_size=1` request, reads `count`; walks pages and reports a floor if the endpoint has none); `--group_by day\|week\|month\|FIELD` | No | **Yes** | Counts without paging the whole result. |
| `events export`, `observations export`: ER's own CSV from `/activity/events/export/` and `/trackingdata/export/`, unchanged; on 403 falls back to JSON records and says so | No | **Yes** | Bulk pulls without paging JSON. Flags are hand-added because the spec declares none. |
| `--where KEY=VALUE` (repeatable) client-side filter on `event_details` | No | **Yes, small** | Cheap given we already fetch and understand the type schema. |
| 200-page cap and repeated-`next` URL detection, result marked `truncated` with a `note` | No | **Yes, trivial** | Safety net; we page until `next` is null. |
| Glob patterns in event-type names (`*carcass*`); subject-group name → id; subtype miss retried as subject type; nearest-match suggestion on a name miss | Partial (exact values/display names) | Probably | Natural extension of the P1 resolver. |
| `link event\|patrol <uuid>` web-app deep link (`--no-lnglat`); `analyzers subject\|spatial`; `patrols --with_distance` (from the leader's track); `--status` validated locally | No | Maybe | Low cost, modest value; take if a skill asks. |
| `event-types list --api_version v1\|v2`, merging the v1 and v2 lists; `event-categories list` | Partial (`events list event-types --json`, categories) | No new command | Covered by existing commands plus `--json`. |
| Output over 16 KB spilled to a temp file `er-*.json` by default; `-o -` forces stdout; `Done.` line and read hints on **stdout** | No | **No** (or opt-in) | Breaks `er … \| jq`. Our `-o` convention already covers it; `Done.` stays on stderr (§Output contract). |
| argv leniency layer: fills in a missing action, swaps `list`/`search`, `event_types`→`event-types`, `--sort-by`→`--sort_by`, joins negative values to flags, `er get <path>` hint | Partial (list/search aliases, both flag spellings) | **No** | The rest hides typos from the agent; what we have is enough. |
| Workspace extensions: `skills/*/er-<name>.py` under `$OPENCLAW_STATE_DIR/workspace` become `er <name>` | No | **No** | Tied to the OpenClaw workspace layout. |
| Read-only enforcement: GET-only spec index, `_ReadOnlyClient` refusing non-GET | No | **No** | This tool writes by design (§Non-goals). |
| Errors and usage errors mirrored to both stdout and stderr | No | **No** | Settled in §Output contract. |
| `tests/test_cli.py` checks the README's usage examples against the real parser | No | **Yes** | Would have caught the stale `profile use` wrapper docs fixed in PR 34. Process, not feature. |

Otus also dropped `featuresets get` from its registry (used internally
only); we keep ours.

## Design decisions

### Output contract

Every read command gains `--json` (or a global `--format json|table`) and
`-o/--output PATH`. JSON output is exactly er-cli's shape so tusker skills
can adopt it unchanged:

```json
{"records": [...], "meta": {"total": 12, "pages": 1, "count_reported": 12}}
```

- `records` is always a flat list, even for single-object `get` commands
  (one element) and GeoJSON FeatureCollections (one element).
- The ER `{"data": ..., "status": ...}` envelope and DRF `results/next`
  paging are normalized away in one helper.
- `-o` creates parent directories and prints one summary line to stderr
  (`Done. N record(s) written to PATH (P page(s)).`).
- Today's aligned-column output stays the human default; `--json` is
  opt-in. (er-cli defaults to JSON; agents pass `-o` anyway, so the
  default only matters for humans.)

### Bearer-token auth

Add `--token` / `ER_TOKEN` alongside the existing password and cached
session paths, plus a way to store a token on a profile so it can be
token-only.

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

Document er-cli's Docker tip: tokens contain shell-special characters, so
prefer `--env-file` or a bare `-e ER_TOKEN` over `-e ER_TOKEN=$TOKEN`.

### Read-only resource commands

Port er-cli's registry. erclient already exposes most endpoints, so each
command is a thin wrapper:

| Command | erclient method / path |
|---|---|
| `status show` | `_get("status")` |
| `auth whoami` | `get_me()` |
| `subjects search` / `get ID` | `get_subjects(**kw)` / `get_subject(id)` |
| `tracks get SUBJECT_ID --since --until` | `get_subject_tracks(...)` (v2) |
| `observations search` | `get_observations(...)` |
| `events search` / `get ID` | `get_events(**kw)` / `get_event(event_id=...)` |
| `patrols search` / `get ID` | `get_patrols(**kw)` / `_get("activity/patrols/{id}")` |
| `sources search` / `get ID` | `get_sources()` / `get_source_by_id(id)` |
| `subject-groups list` / `get ID` | `get_subjectgroups(...)` / `_get("subjectgroup/{id}")` |
| `subject-sources search --subjects --sources` | `_get("subjectsources", params=...)` (er-cli's `v1.0_subjectsources_list`; the erclient helper `get_subjectsources(subject_id)` hits a different path) |
| `spatial-feature-groups list` / `get ID` | `_get("spatialfeaturegroup")` / `_get("spatialfeaturegroup/{id}")` — er-cli called this resource `fences`; renamed because groups hold roads, water points and boundaries as well as geofences |
| `spatial-features list` / `get ID` | `_get("spatialfeature")` / `_get("spatialfeature/{id}")` |
| `featuresets list` / `get ID` | `_get("featureset")` / `_get("featureset/{id}")` |
| `regions list` | `_get("regions")` |

**Decided 2026-10-07 — spec-derived flags: no.** er-cli's idea was that
re-bundling `openapi.yaml` updates every command's flags with no code
change. In practice every read command shipped since P0 (PR 18, PR 23:
seventeen rows) used the hand-written `Flag` registry, and three review
rounds showed why that is the better fit: the flags that mattered needed
per-flag knowledge the OpenAPI spec does not carry — which multi-value
params das reads with `getlist()` (repeated params) versus `CSVWidget`
(one comma-joined value), which numeric params das parses with `int()`,
which endpoints are unpaginated `APIView`s with their own envelope. A
14k-line bundled spec would have generated the wrong encodings for all of
those and still needed a per-row override table. Hand-written rows stay;
each row is one `ReadCommand` with its flags, and endpoint quirks live on
the row (`prepare`, `unwrap`).

### Pagination and retries

- Generalize `client._collect_pages` into one helper used by every list
  command: follow `next`, stop at `--limit`, default `page_size=100`,
  report `pages` in meta.
- Retries with exponential backoff on 429/5xx and network errors for
  reads. erclient's `_get` retries but with fixed sleeps and no 429
  handling; consider `max_http_retries` plus a thin wrapper for
  `_get`-path commands.

### Naming

er-cli uses top-level `event-types list` and `event-categories list`;
this repo nests them as `events list event-types` / `events list
categories`, and uses `list` where er-cli uses `search`.

**Decided 2026-10-06 (PR 23).** Every paginated read command answers to
both `list` and `search`, as visible aliases of one Command; the row's own
name is the one in `--help`. `events list` stays the authoring sub-group
and is not aliased. Event objects keep this repo's nesting; everything new
uses er-cli's flat `<resource> <action>`. PR 23 also renamed er-cli's
`fences` to `spatial-feature-groups` (a group holds roads, water points
and boundaries as well as geofences; nothing in das is called a fence) and
added `spatial-features list|get` and `featuresets list`.

## Todo

### P0 — the agent contract

- [x] Uniform JSON output (`--json`, `-o/--output`) on every read command,
      er-cli's exact `{records, meta}` shape. (2026-09-23,
      `docs/superpowers/plans/2026-09-23-read-surface.md`)
- [x] Bearer-token auth path (`--token`, `ER_TOKEN`, `auth login --token`);
      precedence decided — see §Bearer-token auth.
- [x] Read-only resource commands per the table above — hand-written flags;
      spec-derived flags remain the P1 decision. (2026-09-23)
- [x] Shared pagination helper with `--limit` and envelope unwrapping
      (`read.fetch` / `read.follow_pages`; choices use it too). (2026-09-23)

### P1 — robustness and ergonomics

- [x] Decide on spec-derived flags for the read surface: **no**, see
      §Read-only resource commands. (2026-10-07)
- [x] Event-type name → id resolution on `events search --event-type`
      (values, display names case-insensitively, UUIDs, comma-mixed; one
      event-types listing per invocation, none when only ids are given).
      (2026-10-07) `events post` needs nothing: das's event write takes the
      type *value*, not an id.
- [x] Retries with backoff on 429/5xx/network for reads. (2026-10-07) A
      GET-only urllib3 `Retry` mounted on erclient's session: 3 retries
      (1 for connect, so a mistyped server fails fast), 0/2/4 s backoff,
      `Retry-After` honoured up to 30 s, each retry noted on stderr, writes
      never replayed; erclient's own fixed-sleep loop stays disabled so
      nothing retries twice.
- [x] `--version` flag. (2026-10-07)
- [x] Show `[GET /api/v1.0/...]` in each read command's `--help`. (2026-09-23,
      shipped with the read surface)
- [x] Command-naming aliases per the Naming section. (2026-10-06, PR 23 —
      plus the `fences` rename and the `observations search` guard, see
      §Hardening.)

### Hardening (not in the original phases; driven by early users and review)

Work that kept the shipped surface honest rather than extending it. Listed
so the phases above don't suggest the repo was idle between P0 and P1.

- [x] `--server` accepts hostnames and URLs, not just site names
      (`sandbox.pamdas.org` no longer becomes `sandbox.pamdas.org.pamdas.org`);
      canonical lower-cased host for identity comparisons; junk stored
      servers are tolerated. (PR 9)
- [x] Token-auth follow-ups: every `/user/me/` outcome has a clean error,
      401s on writes carry the credential remedy, static records never hit
      the refresh path. (PR 10)
- [x] `events search` multi-value filters as repeated params; integer
      `--max-gap-minutes`. (PR 18 review)
- [x] `observations search` requires exactly one selector and defaults
      `--since` to 24 h before `--until` (or now), converted to UTC; the
      guard runs before any connection. Without it a bare call paged
      through every observation on the site. (PR 23)
- [x] `featuresets list` unwraps das's `{"features": [...]}` envelope so
      `--limit` and `meta` are right. (PR 23)
- [x] **`events post` validates event details against the type's schema
      before sending** (2026-10-07, PR 26). das deliberately does not
      validate `event_details` on write (`schemas/submission.py` says so), so
      a wrong field key, a non-choice select value, a string where a number
      is expected, or a missing required field was stored silently. The CLI
      fetches ER's own rendered schema once per type (v2:
      `eventtypes/<v>/schema?pre_render=true&s_format=enum`, choices inlined
      as enums, strictness re-added; v1: the legacy rendered envelope) and
      checks every event with `jsonschema` before any post; `--no-validate`
      skips it. Raised by an early user.
- [ ] erclient's token refresh uses module-level `requests.post`, not the
      session, so the read retry policy does not cover `/oauth2/token`: a
      single 502 there reads as "cached session expired — run er auth login".
      Pre-existing; surfaced in the PR 30 review. Needs an erclient change
      or a refresh wrapper here.
- [ ] erclient's convenience methods (`get_event_types`, `get_events`, …)
      used by the authoring commands and by name resolution build their own
      paths and still run erclient's fixed-sleep retry loop with no body-read
      retry. Covering them means re-implementing those calls as `get_json`
      paths. Surfaced in the PR 30 review; no one has been bitten.

### P2 — consolidation

- [-] ~~Optional fallback read of `~/.earthranger/config.json`, or a one-off
      `er profile import-legacy`.~~ Declined 2026-10-08: no known user of
      such a file (er-cli was never adopted by a tusker skill). Revisit only
      if one turns up.
- [x] Offline tests mirroring er-cli's. Covered as the surface was built
      (2026-10-08): page normalization and envelope unwrap (`test_read.py`),
      paginated fetch with a mocked client (`test_read.py`,
      `test_read_commands.py`), name resolution (`test_read_commands.py`),
      missing-credentials text (`test_cli.py`). No separate suite needed.
- [x] README section "How agent skills use it" (2026-10-08): the write-to
      `-o` then read-the-file pattern, four one-liners, and what a skill can
      rely on — exit codes, bounded requests, retries, validated posts,
      sandbox auth.
- [-] Retire `packages/er-cli` in tusker. Not done from this repo: tusker is
      owned by another team. Everything er-cli offered now exists here (this
      spec's P0/P1), so the retirement is a one-PR change on their side when
      they choose to make it.

### P3 — otus parity (proposed 2026-10-09, see §Comparison with otus er-cli)

Ordered by value to an agent skill. Each item is additive to the read
surface; none touches the DSL or writes.

- [x] `--fields` projection and `--format json|tsv|csv` on every read
      command (tsv/csv require `--fields`). Dotted paths and list indexes
      as otus spells them, so skills port unchanged.
- [x] Site-timezone windows: `--today`, `--yesterday`, `--last <N>d|h` on
      events, patrols, observations, tracks (and both exports); `er now`.
      (2026-10-09) **Decided during implementation:** the extra GET /status
      is made only when one of those flags, a period `--group-by`, `er
      now`, or a bare `--since`/`--until` date on observations, tracks or an
      export (which must be sent with the site's offset) needs it — every
      other read never pays for it — and `server_utc`, `site_now`,
      `site_tz` appear in `meta` only on those commands. Otus adds them to
      every response; we chose not to double every read's request count
      for a value `er now` gives in one call. A bare date is the site's
      calendar day on every command (review round 5, 2026-10-09).
- [x] `--count-only` and `--group-by day|week|month|FIELD` on list commands.
- [x] `events export` and `observations export` (server CSV, pass-through;
      403 → JSON fallback with a `note:`).
- [x] `--where KEY=VALUE` on `events search`.
- [x] Page cap (200) and repeated-`next` detection in `read.follow_pages`,
      surfaced as `meta.truncated` + `meta.note`.
- [x] Glob matching in `--event-type`; subject-group name resolution on
      `subjects search --subject-group`; nearest-match suggestion on misses.
- [x] Test that every `er …` example in README.md parses against the real
      click command tree.
      (All ticked items: 2026-10-09, `feat/otus-parity`.)
- [ ] Maybe: `link event|patrol ID`, `analyzers subject|spatial`,
      `patrols --with-distance`.

Declined (recorded so they are not re-proposed): temp-file spill of large
stdout, argv leniency beyond the existing aliases, workspace extensions,
read-only client, errors on both streams.

Guardrails for the authoring surface (the DSL, `apply`, `pull`, and the
human output of `events list`/`events show` stay exactly as they are):

- `--fields` / `--format` on `events list event-types`, `events list
  categories` and `events show event-type` act only under `--json` or
  `-o`; the default human output is untouched, and `pull`'s YAML is not a
  read-command output and never gains them.
- Glob matching and nearest-match suggestions apply to `events search
  --event-type` (and `subjects search --subject-group`) only. `events post
  --event-type` keeps passing the exact value to ER; `apply` and `pull`
  take values from the spec and never resolve patterns.
- The README-examples test only parses each `er …` example against the
  click command tree. It never connects or runs `apply`/`pull`;
  placeholder file names in examples are fine.

## Testing

- Every new read command: offline test with a mocked client asserting
  the `{records, meta}` shape, pagination stop at `--limit`, and envelope
  unwrap.
- Token auth: precedence table exercised end to end through `_connect`.
- If spec-derived flags are adopted: a test that each registered
  operation's query params appear as click options.

## References

- otus er-cli source (current target): `~/padas/otus/packages/er-cli/src/er/cli/`
  — `resources.py` (registry), `main.py` (flag derivation, windows,
  counting, leniency), `http.py` (pagination, retries, exports, name
  resolution), `output.py` (`--fields`, formats), `config.py`; skills that
  call it: `~/padas/otus/agents/otus/skills/*/SKILL.md`.
- tusker er-cli source (superseded): `~/padas/tusker/packages/er-cli/src/er/cli/` —
  `resources.py` (registry), `spec.py` (OpenAPI index), `http.py`
  (pagination, retries, name resolution), `output.py`, `config.py`.
- Prior design: `docs/superpowers/specs/2026-08-31-er-events-cli-design.md`.
- Backlog pointer: `docs/backlog/er-cli-parity.md`.
