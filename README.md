# earthranger-cli

A command-line utility for creating and **editing** EarthRanger event
categories, choices, and v2 event types — and posting events — directly
against the EarthRanger API, authenticated with a username and password.

You describe what you want in a small YAML spec (no hand-written JSON
Schema); `er events apply` generates the ER v2 schema envelope and the
shared Choice records, then idempotently creates what's missing and
patches what changed. Nothing is ever deleted — removal means
`is_active: false`.

<img width="1190" height="582" alt="er-cli-demo" src="https://github.com/user-attachments/assets/8ec645fb-80ce-4206-8dc0-f6509bb36aa8" />

## Install

`earthranger-cli` is published on [PyPI](https://pypi.org/project/earthranger-cli/)
and requires Python 3.11 or newer. It provides one command, `er`.

The recommended install is as an isolated tool, so `er` lands on your
`PATH` without touching any project's virtualenv:

```bash
uv tool install earthranger-cli    # or: pipx install earthranger-cli
er --help
```

To upgrade later: `uv tool upgrade earthranger-cli` (or `pipx upgrade earthranger-cli`).

A plain `pip install earthranger-cli` into whatever environment you have
active also works. To try it once without installing anything:

```bash
uvx --from earthranger-cli er --help
```

### From source (development)

Clone the repo and install it in editable mode with the dev extras
(`pytest`, `ruff`); changes to the source take effect immediately:

```bash
git clone https://github.com/PADAS/earthranger-cli.git
cd earthranger-cli
uv sync --extra dev          # creates .venv with the package installed editable
uv run er --help             # or: source .venv/bin/activate && er --help
uv run pytest -q
```

If you'd rather install editable into an environment you already manage:

```bash
uv pip install -e ".[dev]"
```

Releases are cut by pushing a `vX.Y.Z` tag that matches `__version__` in
`src/earthranger_cli/__init__.py`; the `release` GitHub Actions workflow
runs the tests, builds with `uv build`, and publishes to PyPI via Trusted
Publishing.

## Authenticate

Sessions live on profiles (gcloud-style): create a profile, log in once,
and every command run under that profile reuses its cached session.

```bash
er profile add myreserve --server myreserve --username me   # becomes the default profile
er auth login                 # prompts for your password
```

Working across sites? Save each as a profile; `er profile use` picks the
default, and a flag or env var overrides it per command or per shell:

```bash
er profile add sandbox --server sandbox --username me
er profile add prod --server myreserve --username me   # a new profile becomes the default
er profile use sandbox                 # make sandbox the default (all shells)
er profile set username me2           # edit a property on the selected profile
er --profile sandbox events list categories   # one-off override
export ER_PROFILE=prod                 # pin this shell, regardless of the default
er profile list                        # marker shows this invocation's selection
er profile current                     # prints it (exit 1 if none) — prompt-friendly
er profile use                         # clear the default
```

Selection order is `--profile`, then `ER_PROFILE`, then the default from
`er profile use`. The selected profile supplies the server and default
username when you don't pass them; explicit `--server`/`--username` flags
always win. The default is stored in `~/.config/er-events/config.json`
and is shared by every shell, so pin a terminal with `ER_PROFILE` when you
need it to stay on one site. Whenever `er profile use` or `er profile add`
changes the default, they tell you on stderr if the current shell has
`ER_PROFILE` set and so won't follow it. Re-adding an existing profile (say,
to fix its server) edits it in place and leaves the default alone.

`auth login` requires a selected profile: it verifies your credentials
and caches the access and refresh tokens (never your password) in
`~/.config/er-events/tokens/<profile>.json` (0600). Subsequent commands
under that profile just work — expired access tokens are refreshed
automatically and the rotated tokens re-cached. `er auth status` shows
the selected profile's session; `er auth logout` deletes it. The session
is bound to the profile's identity: `profile set server`, setting
`username` to a different user, or `profile remove` all clear it, and
logging in with a different `--username` updates the profile to match.
Profiles pointing at the same server hold independent sessions.

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

If the cached session's refresh token has expired, commands fail with
`error: cached session for profile '<name>' expired or invalid — run
'er auth login'`. Without a selected profile there is no session cache —
bare `--server` one-offs authenticate with `--password`/`ER_PASSWORD`
each time. (Upgrading from host-keyed caches: run `er auth login` once
per profile; old `tokens/<host>.json` files are ignored.)

## Walkthrough

1. Write a spec (start from `examples/wildlife_monitoring.yaml`):

   ```yaml
   category:
     value: wildlife_monitoring
     display: Wildlife Monitoring
   event_types:
     - value: animal_sighting
       display: Animal Sighting
       fields:
         - key: species
           label: Species
           type: select
           options: [elephant, lion]
         - key: count
           label: Number of animals
           type: integer
           min: 0
       required: [species]
   ```

2. Preview what would change, then apply:

   ```bash
   er events apply spec.yaml --dry-run
   er events apply spec.yaml
   ```

3. Post an event against the new type:

   ```bash
   er events post --event-type animal_sighting \
       --field species=elephant --field count=3 \
       --location -1.286,36.817
   ```

   Before anything is sent, the event's details are checked against the
   type's schema as ER renders it: unknown fields, values outside a
   select's choices, wrong types, out-of-range numbers and missing
   required fields are all reported, one line each, and nothing is
   posted. ER's API does **not** make these checks itself (its form
   builder does, in the browser), so without this a typo like
   `--field speices=elephant` would be stored silently. `--no-validate`
   skips the check when you mean to post something the schema doesn't
   describe.

4. Edit: change the spec (rename a display, add a field, drop an
   option), dry-run again, re-apply. `apply` patches exactly what
   differs; dropped options are deactivated, never deleted.

5. Absorb server-side edits into your file: if someone changed a value
   in ER's UI, `er events pull wildlife_monitoring -o spec.yaml`
   rewrites your spec from the live server; `apply --dry-run` should
   then report all `unchanged`. The few constructs the DSL can't
   express (location fields, headers, condition operators other than
   `is_exactly`, non-positional section ids, auto-generate schemas,
   pattern validation, choice-list or nested sub-fields inside
   collections) are reported and refused unless you pass
   `--skip-unsupported`, which drops them and lists what was skipped in
   a comment at the top of the file.

## Commands

| Command | What it does |
|---|---|
| `events apply SPEC [--dry-run]` | Upsert category, choices, and event types from a spec |
| `events post --event-type V --field k=v ... [--no-validate]` | Post one event (`--location LAT,LON`, `--time`, `--title`); details are validated against the type's schema first |
| `events post --file events.yaml [--no-validate]` | Post a batch; every event is validated before any is sent; exits 1 if any fail |
| `events list categories` | List categories (inactive included) |
| `events list event-types [--category V]` | List event types |
| `events show event-type V` | Full v2 event-type JSON + its Choice records |
| `events pull CATEGORY [-o FILE] [--skip-unsupported]` | Reconstruct a DSL spec from the server (reverse of apply) |
| `events search [--event-type V[,V...]] [--limit N] [-o F]` | Search events; `--event-type` takes values, display names or ids (mixed is fine); JSON `{records, meta}` output |
| `events get EVENT_ID [-o F]` | One event as a one-record `{records, meta}` document |
| `events list categories\|event-types [--json] [-o F]`, `events show event-type V [--json] [-o F]` | Same as above, opt-in `{records, meta}` output |
| `status show`, `auth whoami` | Server status; the authenticated user |
| `subjects search\|get`, `tracks get SUBJECT_ID`, `observations search --subject-id ID` | Read subjects, tracks (v2 GeoJSON), raw observations (one selector required; `--since` defaults to 24 h before `--until`, or before now) |
| `patrols search\|get`, `sources search\|get`, `subject-groups list\|get`, `subject-sources search` | Read patrols, sources, groups, collar↔subject assignments |
| `spatial-feature-groups list\|get ID`, `spatial-features list\|get ID` | Read spatial feature groups and the features in them (geofences, roads, water points, boundaries) |
| `featuresets list\|get ID`, `regions list` | Read featuresets and their GeoJSON boundaries, operational regions |
| `auth login [--token T]/status/logout` | Cache (password session or static token), inspect, or clear the selected profile's credential |
| `profile add/use/set/show/list/remove/current` | Named site profiles; `use` sets the default and adding a new profile auto-switches to it; `set` edits the selected profile; `--profile NAME` / `ER_PROFILE` override per command or shell |

Every paginated read command answers to both `list` and `search` (`er subjects list`
and `er regions search` both work); the names above are the ones shown in `--help`.

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

### How agent skills use it

The pattern is: run one command with `-o`, then read only the fields you
need from the file. Raw payloads never enter the model's context, stdout
is empty on success, and anything the CLI wants to tell you (a defaulted
time window, a retry) goes to stderr as a `note:` line.

```bash
# Events of a type since a time — the type is its value, display name or id
er events search --event-type geofence_break --updated-since 2026-09-01T00:00:00Z -o /tmp/gf.json

# One subject's track for a window (v2 GeoJSON; one record, a FeatureCollection)
er tracks get <subject_id> --since 2026-06-01T00:00:00Z --until 2026-06-08T00:00:00Z -o /tmp/track.json

# One subject's raw observations for a day — a selector is required, and
# --since defaults to 24 h before --until (or before now) if you omit it
er observations search --subject-id <subject_id> --until 2026-06-02T00:00:00Z -o /tmp/obs.json

# Everything in a small table, capped
er subjects search --limit 500 -o /tmp/subjects.json
```

Then, in the skill, `jq '.records[] | {id, title, time}' /tmp/gf.json` or
the language equivalent.

What a skill can rely on:

- **Exit codes.** `0` with the file written; `2` for a CLI usage error,
  with the reason on stderr. Missing observation selectors are rejected
  before connecting; unknown event-type names are rejected after fetching
  the server's type listings. Most timestamp flags are passed to the API
  for validation. API and auth failures exit `1`; messages handled by the
  CLI's API error wrapper go to stdout, with the credential to fix named
  when it was a 401. Capture both output streams when diagnosing failures.
- **Bounded requests.** `observations search` refuses to run without a
  selector because ER would otherwise return every observation on the
  site. `--limit` stops paging as soon as it is reached.
- **Retries.** Transient failures (429, 5xx, connection or body-read
  errors) are retried three times with 0/2/4 s backoff and a `note:` on
  stderr; `Retry-After` is honoured up to 30 s. Writes are never replayed.
- **Posting is checked.** `er events post` validates details against the
  type's schema before sending, so a skill that builds an event from model
  output gets a per-field error instead of a silently stored typo.
- **Auth in a sandbox** is `ER_SERVER` plus `ER_TOKEN` in the environment,
  no profile and no login; see the Docker note under *Authenticate* for
  passing the token safely.

## Spec reference

Field types: `string`, `textarea`, `integer` (advisory — ER stores it as
`number` on the wire), `number` (both take
optional `min`/`max`), `boolean`, `date`, `datetime`, `url`, `select`,
`multiselect` (both take `options`).

Sub-forms are `type: collection` fields: repeating groups of scalar
sub-fields (`item_name` labels one entry; optional `button_text`,
`item_identifier`, `columns: 2` with sub-field `column: right`,
`min`/`max` item counts, and a `required:` list of sub-field keys):

```yaml
- key: sightings
  label: Sightings
  type: collection
  item_name: sighting
  fields:
    - {key: species_note, label: Species note, type: string}
    - {key: count, label: Count, type: integer}
  required: [species_note]
```

Choice-list or nested-collection sub-fields aren't supported yet and
are refused by name.

Every field takes optional `active: false` (the field is
deprecated/hidden on ER but its historical data remains) and
`description`. Scalar and choice fields additionally take `hint` (ER's
placeholder, max 32 chars; not on `boolean`/`date`/`datetime` or
collections), and scalar fields take `default`
(`string`/`textarea`/`url`/`integer`/`number`/`boolean` only). `string`
fields take `format: url | email | uuid` — the builder's "Format
Validation" (`url` is sent as JSON Schema `uri`).

Options are `{value, display}` mappings or bare strings
(`lion` → value `lion`, display `Lion`); option values are free text
(ER stores them as-is). `options: []` is valid — it deactivates every
option on that field (an all-inactive choice set pulls back to
exactly this). Category and event-type values must match
`[a-z0-9_]+` (they appear in URLs); field keys follow ER's own rule,
`[a-zA-Z0-9_-]+`.

Select/multiselect fields take an optional `choices_field` naming the
exact ER Choice set to use, overriding the derived
`<event_type>_<field_key>` name — needed for stock event types whose
choice sets predate this tool. To share one set across fields, declare
it once at the top level and reference it — the fields then omit
`options` entirely (inline-declared sharing also works when every
sharing field carries identical options):

```yaml
choices:
  shared_actions:
    - {value: stopped, display: Halted}
    - {value: warned, display: Warned, icon: warn_icon}

event_types:
  - value: t1
    fields:
      - {key: action, label: Action, type: select, choices_field: shared_actions}
  - value: t2
    fields:
      - {key: response, label: Response, type: multiselect, choices_field: shared_actions}
```

Options may carry an `icon`, and their spec order is the dropdown
order: apply writes `ordernum` from spec position **only when the
visible order actually differs** (a set whose active options already
appear in spec order keeps its existing numbering — stock 10/20/30
gaps and holes left by deactivated records are left alone). A set with
missing ordernums gets numbered on first apply.

Per event type an optional `layout: {label, columns}` (default
`{label: Details, columns: 1}`) controls the form section; with
`columns: 2`, per-field `column: right` places a field in the right
column. Multi-section forms use `sections:` instead of `fields:`/
`layout:` — a list of `{label?, columns?, active?, condition?, fields: [...]}`
mappings, one per form section, in order. `active: false` hides a
section; `condition: {field, operator: is_exactly, value}` shows it only
when another (outside) field has the given value — the only condition
operator ER's builder offers that the DSL supports so far; the other
operators are refused by name on pull:

```yaml
- value: entry_alert
  display: Entry Alert
  sections:
    - label: ""
      fields: [...]
    - label: ""
      columns: 2
      fields: [...]
```

A form-less event type (e.g. an incident collection container) is
declared with an explicit `fields: []` and, for collections,
`is_collection: true`.

Per event type: `value`, `display`, `fields` (required); optional:
`required`, `is_active`, `icon_id` (sent to ER as its writable `icon`
field — the API's own `icon_id` is a derived, read-only property),
`default_priority` (`gray|green|amber|red` or `0|100|200|300`),
`default_state` (`new|active|resolved`), `readonly` (makes the whole
event type read-only in ER — v2 schemas have no per-field read-only),
`geometry_type` (`point|polygon`; whether events record a location
point or a drawn polygon), `auto_resolve` + `resolve_time` (auto-resolve
events after N hours — `auto_resolve: true` requires `resolve_time`,
since ER silently ignores the flag without it), and `ordernum` (explicit
display rank — a number; ER uses fractional ranks like `0.5` for
insert-between — deliberately not derived from spec position, because a
spec doesn't own every event type in its category). These are sent only when declared: omitted
keys leave the server's values untouched. `geometry_type` is immutable
once an event type exists — apply refuses a spec that declares a
different value than the server's, since ER's API would accept the
change but events already recorded under the old geometry would be
corrupted.

### What apply owns

`apply` only manages what the spec declares: it never touches event
types or categories that exist on the server but aren't in the spec
(retire one by setting `is_active: false` in the spec). The exception
is choice options within a spec-managed field — there the spec is
authoritative, and removed options are deactivated on the server.

### Why YAML and not JSON?

Both work: spec files are parsed with a YAML parser, and YAML is a
superset of JSON, so a pure-JSON spec file is accepted as-is. YAML is
the documented format because spec files are hand-authored and reviewed
— comments matter, and block style keeps nested fields readable. Watch
YAML's implicit typing (`no` → false, `1.10` → a float): quote anything
ambiguous.

## v2 schemas and choices, briefly

ER v2 event types store a `{json, ui}` schema envelope. Dropdown fields
don't embed their options; they reference shared **Choice** records via
`$ref: /api/v2.0/schemas/choices.json?field=<name>`. This tool derives
`<name>` as `<event_type_value>_<field_key>` (compressed with a short
hash suffix when longer than ER's 40-char field limit) and manages those records
for you — creating, re-labelling, deactivating, and reactivating options
to mirror your spec.
