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
er profile add myreserve --server myreserve --username me   # prints: export ER_PROFILE=myreserve
export ER_PROFILE=myreserve   # select it in this shell (the wrapper below automates this)
er auth login                 # prompts for your password
```

Working across sites? Save each as a profile and select one per shell
or per command — selection is never global:

```bash
er profile add sandbox --server sandbox --username me
er profile add prod --server myreserve --username me   # adding auto-switches (via the wrapper)
er profile use sandbox                 # switch this shell (needs the wrapper below)
er profile set username me2           # edit a property on the selected profile
er --profile sandbox events list categories   # one-off override
er profile list                        # marker shows this shell's selection
er profile current                     # prints it (exit 1 if none) — prompt-friendly
```

A selected profile supplies the server and default username when you
don't pass them; explicit `--server`/`--username` flags always win.

`er profile use` and `er profile add` print an `export ER_PROFILE=...`
line on stdout (a subprocess can't set its parent shell's env; add's
confirmation goes to stderr), so add this wrapper to your zshrc — it
evals that line in place, which also makes a newly added profile the
shell's selection immediately, and passes every other command through;
the optional prompt segment shows the shell's selection:

```zsh
er() {
  if [[ $1 == profile && ($2 == use || $2 == add) ]]; then
    local out
    out=$(command er "$@") || { [[ -n $out ]] && print -r -- "$out"; return 1; }
    # eval only the switch protocol; anything else (e.g. --help) prints normally
    if [[ $out == "unset ER_PROFILE" || ($out == "export ER_PROFILE="* && $out != *$'\n'*) ]]; then
      eval "$out"
    elif [[ -n $out ]]; then
      print -r -- "$out"
    fi
  else
    command er "$@"
  fi
}
_er_prompt() { [[ -n $ER_PROFILE ]] && print -n "%F{yellow}(er:$ER_PROFILE)%f "; }
setopt PROMPT_SUBST; PROMPT='$(_er_prompt)'"$PROMPT"
```

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
| `events post --event-type V --field k=v ...` | Post one event (`--location LAT,LON`, `--time`, `--title`) |
| `events post --file events.yaml` | Post a batch; exits 1 if any fail |
| `events list categories` | List categories (inactive included) |
| `events list event-types [--category V]` | List event types |
| `events show event-type V` | Full v2 event-type JSON + its Choice records |
| `events pull CATEGORY [-o FILE] [--skip-unsupported]` | Reconstruct a DSL spec from the server (reverse of apply) |
| `events search [--event-type ID[,ID...]] [--limit N] [-o F]` | Search events; JSON `{records, meta}` output |
| `events get EVENT_ID [-o F]` | One event as a one-record `{records, meta}` document |
| `events list categories\|event-types [--json] [-o F]`, `events show event-type V [--json] [-o F]` | Same as above, opt-in `{records, meta}` output |
| `status show`, `auth whoami` | Server status; the authenticated user |
| `subjects search\|get`, `tracks get SUBJECT_ID`, `observations search` | Read subjects, tracks (v2 GeoJSON), raw observations |
| `patrols search\|get`, `sources search\|get`, `subject-groups list\|get`, `subject-sources search` | Read patrols, sources, groups, collar↔subject assignments |
| `fences list`, `featuresets get ID`, `regions list` | Read geofence groups, GeoJSON boundaries, operational regions |
| `auth login [--token T]/status/logout` | Cache (password session or static token), inspect, or clear the selected profile's credential |
| `profile add/use/set/show/list/remove/current` | Named site profiles; `use` selects per shell and `add` auto-switches (via the wrapper); `set` edits the selected profile; `--profile NAME` per command |

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
