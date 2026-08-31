# er-events-cli

A command-line utility for creating and **editing** EarthRanger event
categories, choices, and v2 event types — and posting events — directly
against the EarthRanger API, authenticated with a username and password.

You describe what you want in a small YAML spec (no hand-written JSON
Schema); `er-events apply` generates the ER v2 schema envelope and the
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
er-events auth login --username me  # prompts for your password
```

`auth login` verifies your credentials and caches the access and refresh
tokens (never your password) in `~/.config/er-events/tokens/<host>.json`
(0600). Subsequent commands on that server just work — expired access
tokens are refreshed automatically and the rotated tokens re-cached.
`er-events auth status` shows the cached session; `er-events auth logout`
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
   er-events apply spec.yaml --dry-run
   er-events apply spec.yaml
   ```

3. Post an event against the new type:

   ```bash
   er-events post-event --event-type animal_sighting \
       --field species=elephant --field count=3 \
       --location -1.286,36.817
   ```

4. Edit: change the spec (rename a display, add a field, drop an
   option), dry-run again, re-apply. `apply` patches exactly what
   differs; dropped options are deactivated, never deleted.

5. Absorb server-side edits into your file: if someone changed a value
   in ER's UI, `er-events pull wildlife_monitoring -o spec.yaml`
   rewrites your spec from the live server; `apply --dry-run` should
   then report all `unchanged`. Constructs the DSL can't express
   (collections, locations, multi-section layouts, hand-named choice
   fields) are reported and refused unless you pass
   `--skip-unsupported`, which drops them and lists what was skipped in
   a comment at the top of the file.

## Commands

| Command | What it does |
|---|---|
| `apply SPEC [--dry-run]` | Upsert category, choices, and event types from a spec |
| `post-event --event-type V --field k=v ...` | Post one event (`--location LAT,LON`, `--time`, `--title`) |
| `post-event --file events.yaml` | Post a batch; exits 1 if any fail |
| `list categories` | List categories (inactive included) |
| `list event-types [--category V]` | List event types |
| `show event-type V` | Full v2 event-type JSON + its Choice records |
| `pull CATEGORY [-o FILE] [--skip-unsupported]` | Reconstruct a DSL spec from the server (reverse of apply) |

## Spec reference

Field types: `string`, `textarea`, `integer` (advisory — ER stores it as
`number` on the wire), `number` (both take
optional `min`/`max`), `boolean`, `date`, `datetime`, `url`, `select`,
`multiselect` (both take `options`).

Every field also takes optional `hint` (ER's placeholder, max 32 chars;
not on `boolean`/`date`/`datetime`), `description`, and `default`
(`string`/`textarea`/`url`/`integer`/`number`/`boolean` only). `string`
fields take `format: url | email | uuid` — the builder's "Format
Validation" (`url` is sent as JSON Schema `uri`).

Options are `{value, display}` mappings or bare strings
(`lion` → value `lion`, display `Lion`). Slugs — category value, event
type values, field keys, option values — must match `[a-z0-9_]+`.

Per event type: `value`, `display`, `fields` (required);
`required`, `is_active`, `icon_id` (optional; sent to ER as its writable
`icon` field — the API's own `icon_id` is a derived, read-only property).

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
