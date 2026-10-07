# er-cli parity — absorbing the agent-facing read surface

Date: 2026-09-02
Status: P0 implemented 2026-09-23; P1/P2 open.

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
| Auth | Username/password → OAuth session cached per profile, auto-refresh, file locking, `ER_PROFILE` + zsh wrapper | Bearer token only: `--token` / `ER_TOKEN` / profile in `~/.earthranger/config.json` with `default_profile`; no refresh |
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
- [ ] Event-type name → id resolution on `events search --event_type`
      (values, display names, UUIDs, comma-mixed); reuse in `events post`.
- [ ] Retries with backoff on 429/5xx/network for reads.
- [ ] `--version` flag.
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
- [ ] **`events post` validates event details against the type's v2
      schema before sending.** das deliberately does not validate
      `event_details` against the JSON schema on write (`schemas/submission.py`
      says so), so a wrong field key, a non-choice select value, a string
      where a number is expected, or a missing required field is stored
      silently and mis-rendered later. Raised by an early user; next up.

### P2 — consolidation

- [ ] Optional fallback read of `~/.earthranger/config.json`, or a one-off
      `er profile import-legacy` — only if anyone has such a file.
- [ ] Offline tests mirroring er-cli's: DRF page normalization, envelope
      unwrap, paginated fetch with a mocked transport, name resolution,
      missing-credentials error text.
- [ ] README section "How agent skills use it" with the one-liner pattern
      (`er events search --event_type geofence_break --updated_since … -o /tmp/gf.json`).
- [ ] Retire `packages/er-cli` in tusker: point its Dockerfile/requirements
      at this repo, delete the package, keep the README's skill guidance.

## Testing

- Every new read command: offline test with a mocked client asserting
  the `{records, meta}` shape, pagination stop at `--limit`, and envelope
  unwrap.
- Token auth: precedence table exercised end to end through `_connect`.
- If spec-derived flags are adopted: a test that each registered
  operation's query params appear as click options.

## References

- er-cli source: `~/padas/tusker/packages/er-cli/src/er/cli/` —
  `resources.py` (registry), `spec.py` (OpenAPI index), `http.py`
  (pagination, retries, name resolution), `output.py`, `config.py`.
- Prior design: `docs/superpowers/specs/2026-08-31-er-events-cli-design.md`.
- Backlog pointer: `docs/backlog/er-cli-parity.md`.
