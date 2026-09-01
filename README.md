# earthranger-cli

A command-line utility for creating and **editing** EarthRanger event
categories, choices, and v2 event types — and posting events — directly
against the EarthRanger API, authenticated with a username and password.

You describe what you want in a small YAML spec (no hand-written JSON
Schema); `er events apply` generates the ER v2 schema envelope and the
shared Choice records, then idempotently creates what's missing and
patches what changed. Nothing is ever deleted — removal means
`is_active: false`.

## Install

```bash
uv pip install -e .
```

## Authenticate

The easiest way: log in once and let the CLI cache your tokens.

```bash
export ER_SERVER=myreserve          # site name, or a full https:// URL
er auth login --username me  # prompts for your password
```

Working across sites? Save each as a profile and select one per shell
or per command — selection is never global:

```bash
er profile add sandbox --server sandbox --username me
er profile add prod --server myreserve --username me
er profile use prod                    # this shell targets prod (needs the wrapper below)
er --profile sandbox events list categories   # one-off override
er profile list                        # marker shows this shell's selection
er profile current                     # prints it (exit 1 if none) — prompt-friendly
```

A selected profile supplies the server and default username when you
don't pass them; explicit `--server`/`--username` flags always win.

`er profile use` prints an `export ER_PROFILE=...` line (a subprocess
can't set its parent shell's env), so add this wrapper to your zshrc —
it evals that line in place and passes every other command through; the
optional prompt segment shows the shell's selection:

```zsh
er() {
  if [[ $1 == profile && $2 == use ]]; then
    local out; out=$(command er "$@") || { [[ -n $out ]] && print -r -- "$out"; return 1; }
    eval "$out"
  else
    command er "$@"
  fi
}
_er_prompt() { [[ -n $ER_PROFILE ]] && print -n "%F{yellow}(er:$ER_PROFILE)%f "; }
setopt PROMPT_SUBST; PROMPT='$(_er_prompt)'"$PROMPT"
```

`auth login` verifies your credentials and caches the access and refresh
tokens (never your password) in `~/.config/er-events/tokens/<host>.json`
(0600). Subsequent commands on that server just work — expired access
tokens are refreshed automatically and the rotated tokens re-cached.
`er auth status` shows the cached session; `er auth logout`
deletes it. One cache per server host: logging in again (as anyone)
replaces it.

You can always bypass the cache with an explicit password — it takes
precedence when present:

```bash
export ER_USERNAME=me
export ER_PASSWORD=...              # or --password; omit both to be prompted
```

If the cached session's refresh token has expired, commands fail with
`error: cached session for <host> expired or invalid — run 'er-events
auth login'`.

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
   then report all `unchanged`. Constructs the DSL can't express
   (headers, conditional sections, non-positional section ids,
   auto-generate schemas, inactive layout sections, pattern validation,
   deprecated fields) are reported and refused unless you pass
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
| `auth login/status/logout` | Cache/inspect/clear the token for the current `--server` |
| `profile add/use/show/list/remove/current` | Named site profiles; `use` selects per shell (via the wrapper), `--profile NAME` per command |

## Spec reference

Field types: `string`, `textarea`, `integer` (advisory — ER stores it as
`number` on the wire), `number` (both take
optional `min`/`max`), `boolean`, `date`, `datetime`, `url`, `select`,
`multiselect` (both take `options`).

Every field also takes optional `active: false` (the field is
deprecated/hidden on ER but its historical data remains), `hint` (ER's
placeholder, max 32 chars; not on `boolean`/`date`/`datetime`),
`description`, and `default`
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
