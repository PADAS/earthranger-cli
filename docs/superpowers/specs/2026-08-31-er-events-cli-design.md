# er-events-cli — Design

Date: 2026-08-31
Status: Approved design, pre-implementation

## Overview

`er-events-cli` is a standalone command-line utility for creating and
editing EarthRanger event categories, choices, event types (v2 schemas),
and for posting events — operating directly against the EarthRanger API,
authenticated with a username and password.

Users author a friendly YAML spec (a field DSL — no hand-written JSON
Schema) and `apply` it. The tool generates the ER v2 event-type schema
envelope and the shared Choice records, then upserts everything
idempotently: create what's missing, patch what changed, report what it
did. Read-only commands support the edit loop; `post-event` sends events
against the resulting types.

### Goals

- Declarative create/edit of: one event category, its choices, and its
  event types, from a single YAML spec file.
- v2 event-type schemas only (JSON Schema 2020-12 envelope with `json` +
  `ui` sections, choices referenced via `$ref`).
- Idempotent `apply` with `--dry-run` diff preview.
- Post events (single via flags, batch via file).
- Username/password authentication.

### Non-goals

- v1 event-type schemas (inline enums).
- Deleting objects (ER soft-deletes; we only flip `is_active`).
- Managing subjects, patrols, users, or any non-event objects.
- ~~Round-tripping existing ER schemas back into the DSL~~ — superseded:
  the `pull` command reconstructs a DSL spec from server state for
  everything the DSL can express, and reports (or, with
  `--skip-unsupported`, drops) what it cannot.
- Multi-category specs (one spec file = one category; run twice for two).

## Dependencies & packaging

- New repo at `~/padas/er-events-cli`, src layout, `pyproject.toml`,
  managed with uv. Python ≥3.11.
- Runtime deps: `earthranger-client` (PyPI, ≥1.16.0), `click`, `pyyaml`.
- Dev deps: `pytest`, `ruff`.
- Console script: `er` (with the event commands under `er events`).

`earthranger-client`'s `ERClient` provides `post_event_category`,
`patch_event_category`, `post_event_type(version="v2.0")`,
`patch_event_type(version="v2.0")` (v2 PATCH path uses the value slug),
`get_event_categories`, `get_event_types(include_schema=True,
version=...)`, and `post_event`. Choices have no first-class methods;
we use the client's generic `_get`/`_post`/`_patch` path methods against
`choices` (the `/api/v1.0/choices/` endpoint), the same pattern
er-smart-sync uses.

## Authentication & connection

`ERClient` is constructed with:

- `service_root = f"{server}/api/v1.0"`
- `token_url = f"{server}/oauth2/token"`
- `client_id = "das_web_client"`
- `username`, `password`

Connection parameters resolve in priority order:

1. CLI flags: `--server`, `--username`, `--password`
2. Env vars: `ER_SERVER`, `ER_USERNAME`, `ER_PASSWORD`
3. If username is present but password is not: interactive hidden prompt
   (`click.prompt(hide_input=True)`).

`--server` accepts either a bare site name (`myreserve` →
`https://myreserve.pamdas.org`) or a full `https://` URL.

Beyond one-shot password auth: `er auth login` exchanges a
password for OAuth tokens once and caches them (never the password)
per server host in `~/.config/er-events/tokens/<host>.json`;
subsequent commands on that host reuse the cache automatically
(refreshing an expired access token via its refresh token), skipping
it only when an explicit `--username` doesn't match the cached
session's owner. `auth status`/`auth logout` inspect/clear the cache.
`profile add/use/list/remove` manage named `{server, username}` pairs
so switching between sites is one flag (`--profile NAME`) or one
command (`profile use NAME`) instead of re-typing `--server`.

## The spec DSL

One YAML file declares one category and its event types:

```yaml
category:
  value: wildlife_monitoring
  display: Wildlife Monitoring

event_types:
  - value: animal_sighting
    display: Animal Sighting
    is_active: true            # optional, default true
    icon_id: mammal_rep        # optional; sent as ER's writable `icon` field
    fields:
      - key: species
        label: Species
        type: select           # generates choices + $ref
        options:
          - {value: elephant, display: Elephant}
          - lion               # shorthand: value=lion, display=Lion
      - key: count
        label: Number of animals
        type: integer
        min: 0                 # optional; also max
      - key: threats
        label: Observed threats
        type: multiselect
        options: [poaching, snares, habitat_loss]
      - key: notes
        label: Notes
        type: textarea
    required: [species]        # optional
```

Validation rules (fail fast, before any API call):

- `category.value`, `category.display` required.
- Each event type: `value`, `display`, non-empty `fields` required.
- Field `key`s unique within an event type; `value`s unique across
  event types; option `value`s unique within a field.
- `type` must be a supported type; `options` required for
  `select`/`multiselect` and forbidden otherwise; `min`/`max` only on
  `integer`/`number`.
- Every entry in `required` must name a declared field key.
- Values used as slugs (`category.value`, event type `value`) must
  match `[a-z0-9_]+` (we validate, not transform — explicit beats
  magic). Field `key`s follow ER's own looser rule instead,
  `[a-zA-Z0-9_-]+` (das `FORM_ELEMENT_SEGMENT_PATTERN`) — stock event
  types use hyphens and uppercase in field keys.
- Option `value`s are free text, not slugs: ER's `Choice.value` is an
  unconstrained varchar(100) (truncated at generation time), so no
  slug rule applies to them.
- Option shorthand (`- lion`) derives `value: lion`,
  `display: "Lion"` (underscores → spaces, title-cased).

Parsed into small dataclasses (`Spec`, `CategorySpec`, `EventTypeSpec`,
`FieldSpec`, `OptionSpec`) — plain stdlib `dataclasses`, no pydantic
needed at this size.

Beyond the single-section form shown above: an event type can declare
`layout: {label, columns}` plus per-field `column: right` for a
two-column single section, or `sections:` (a list of `{label?,
columns?, fields: [...]}` mappings) for a multi-section form — each
section becomes its own numbered `section-N` in the wire schema, and
`sections:` is mutually exclusive with top-level `fields:`/`layout:`.
A select/multiselect field may set `choices_field` to name an existing
ER Choice set explicitly instead of the derived
`<event_type>_<field_key>` name — needed for stock event types whose
choice sets predate this tool, and to deliberately share one choice
set between fields (allowed only when every sharing field declares
identical options). A form-less event type (e.g. an incident
collection container) is declared with an explicit `fields: []` and,
for collections, `is_collection: true`.

### Why YAML and not JSON?

Anticipated user question; the README will carry this answer too.

- JSON is accepted: spec files are parsed with `yaml.safe_load`, and
  YAML is a superset of JSON, so a pure-JSON spec file works as-is.
  This is a documented, tested promise, not an accident of the parser.
- YAML is the primary format because the spec file is hand-authored,
  versioned, and reviewed — comments matter (JSON has none), and block
  style plus inline lists (`options: [poaching, snares]`) keep nested
  field definitions readable and diffs clean.
- Precedent: human-authored config in this ecosystem is YAML
  (er-smart-sync's `sync.yaml`, GitHub Actions, k8s); JSON remains the
  machine-facing format, which is why `show event-type` emits JSON.
- YAML's implicit-typing gotchas (`no` → false, `1.10` → float) are
  mitigated by slug validation and by quoting ambiguous scalars in the
  shipped examples.

## Field type mapping

Each DSL field generates a `json.properties` entry and a `ui.fields`
entry (each UI field carries `parent: <its section's id>` — `section-1`
for the default single-section form; a multi-section form via
`sections:` numbers sections positionally, `section-1`, `section-2`,
...):

Every json property also carries `"deprecated": false` — ER's meta-schema
(das `eventtype_meta_schemas.py`) requires it on every field variant.

| DSL type      | json property                                          | ui field                                  |
|---------------|--------------------------------------------------------|-------------------------------------------|
| `string`      | `{"type": "string", "title": ...}`                     | `TEXT` / inputType `SHORT_TEXT`            |
| `textarea`    | `{"type": "string", "title": ...}`                     | `TEXT` / inputType `LONG_TEXT`             |
| `integer`     | `{"type": "number", "title": ..., "minimum"/"maximum" if set}` (ER has no integer variant; integer is advisory in the DSL) | `NUMERIC` |
| `number`      | `{"type": "number", ...}` (min/max as above)           | `NUMERIC`                                  |
| `boolean`     | `{"type": "boolean", "title": ...}`                    | `BOOLEAN`                                  |
| `date`        | `{"type": "string", "format": "date", "title": ...}`   | `DATE_TIME`                                |
| `datetime`    | `{"type": "string", "format": "date-time", "title": ...}` | `DATE_TIME`                             |
| `url`         | `{"type": "string", "format": "uri", "title": ...}`    | `TEXT` / inputType `SHORT_TEXT`            |
| `select`      | `{"type": "string", "title": ..., "anyOf": [{"$ref": REF}]}` | `CHOICE_LIST` / `DROPDOWN`           |
| `multiselect` | `{"type": "array", "title": ..., "uniqueItems": true, "items": {"type": "string", "anyOf": [{"$ref": REF}]}}` | `CHOICE_LIST` / `DROPDOWN` |

Where `REF = f"/api/v2.0/schemas/choices.json?field={choice_field}"`. The
CHOICE_LIST ui field is exactly `{"type": "CHOICE_LIST", "inputType":
"DROPDOWN", "parent": "section-1"}` — ER removed the legacy builder-only
`choices`/`EXISTING_CHOICE_LIST` block from its meta-schema (ERA-13201)
and now rejects it; the json `$ref` is the sole linkage to Choice records.

`choice_field = f"{event_type_value}_{field_key}"` when that fits ER's
varchar(40) `Choice.field` column; longer names are compressed to exactly
40 chars — a 31-char readable prefix plus an 8-char sha1 digest of the
full name — so distinct long fields stay distinct and re-applies derive
the same name deterministically. (Choice `value`/`display` have the
larger varchar(100) limit.)

### The v2 envelope

The generated event-type payload:

```json
{
  "value": "...", "display": "...", "category": "<category value>",
  "is_active": true,
  "schema": {
    "json": {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "type": "object",
      "unevaluatedProperties": false,
      "properties": { ... },
      "required": [ ... ]
    },
    "ui": {
      "fields": { ... },
      "headers": {},
      "order": ["section-1"],
      "sections": {
        "section-1": {
          "label": "Details", "columns": 1, "isActive": true,
          "leftColumn": [{"name": <key>, "type": "field"}, ...],
          "rightColumn": []
        }
      }
    }
  }
}
```

Definition order = field order in the spec.

**Known gotcha (inherited from er-smart-sync):** ER's GET returns v2
schemas with `additionalProperties` instead of the
`unevaluatedProperties` marker its POST/PATCH meta-schema requires. When
diffing an existing type against a generated one, normalize the fetched
schema first (restore `unevaluatedProperties`, drop
`additionalProperties`) so equality comparison and re-PATCH both work.

## Choice records

Each `select`/`multiselect` field's options become Choice records:

```json
{"model": "activity.event", "field": "<choice_field>",
 "value": "<option value>", "display": "<option display>",
 "is_active": true}
```

`value` and `display` are truncated to 100 chars (ER varchar(100)).

Upsert per field:

1. Fetch existing: paged `_get("choices", params={"field": ...})`.
2. Missing option → POST.
3. Present but different `display` or `is_active` → PATCH
   (`choices/{id}`).
4. Existing choice not in the spec → PATCH `is_active: false`
   (deactivate, never delete — historical events reference them).
5. Spec option matching an inactive existing choice → reactivate.

## Commands

### `er events apply SPEC.yaml [--dry-run]`

The workhorse and the editing path. Order of operations:

1. Parse + validate spec (all errors reported at once, then exit 1).
2. Fetch category by value → create if missing, patch if `display`
   differs.
3. Upsert choices for every select/multiselect field (before event
   types, so `$ref`s resolve).
4. Fetch existing event types (`include_inactive=True,
   include_schema=True, version="v2"`); for each spec type: POST if
   missing; PATCH (v2, slug path) if the generated payload differs from
   the normalized existing one; no-op otherwise.
5. Print a summary table: object, action (created / updated / unchanged
   / deactivated), detail.

Scope of ownership: `apply` only manages what the spec declares. Event
types or categories on the server that are absent from the spec are
never touched (to retire an event type, set `is_active: false` in the
spec explicitly). The one exception is choice *options* within a
spec-managed field: the spec is authoritative for a field's option set,
so options removed from the spec are deactivated (step 4 of the choices
upsert).

`--dry-run` performs all GETs and the full diff, prints the same summary
with a `would-*` prefix, and performs no writes.

### `er events post`

```
er events post --event-type animal_sighting \
    --field species=elephant --field count=3 \
    [--location -1.286,36.817] [--time 2026-08-31T12:00:00Z] \
    [--title "Elephant sighting"]
er events post --file events.yaml
```

- `--field key=value`, repeatable. Values parsed as YAML scalars so
  `3` → int, `true` → bool, `[a,b]` → list (multiselect).
- Payload: `{"event_type": ..., "time": <now if omitted>,
  "event_details": {...}, "location": {"latitude": ..., "longitude": ...}}`.
- `--file` takes a YAML list of event objects in the same shape
  (`event_type`, `event_details`, optional `location`/`time`/`title`)
  and posts each, reporting per-event success/failure and exiting
  non-zero if any failed.
- No client-side schema validation of field values (the server
  validates); we only coerce scalar types.

### Read-only helpers

- `er events list categories` — value, display, is_active.
- `er events list event-types [--category VALUE]` — value, display,
  category, is_active.
- `er events show event-type VALUE` — the full v2 event-type JSON
  (schema included) plus the Choice records for every `$ref` field it
  references. Output is JSON (pipe-friendly).

### `er events pull CATEGORY [-o FILE] [--skip-unsupported]`

The reverse of `apply`: reconstructs a DSL spec from a live category's
event types and choices, for everything the DSL can express. Anything
it cannot (e.g. headers, conditional sections, non-positional section
ids, auto-generate schemas, inactive layout sections, pattern
validation, deprecated fields) is reported as a warning and, by
default, refused; `--skip-unsupported` drops those pieces instead and
lists what was skipped in a comment at the top of the written file.
Round-tripping a pulled spec straight back through `apply --dry-run`
should report all `unchanged`.

## Module layout

```
src/er_events_cli/
    __init__.py
    cli.py          # Click group + subcommands; thin — parsing flags,
                    # wiring, output formatting
    client.py       # ERClient construction from flags/env; choices
                    # path helpers (get/post/patch wrappers)
    dsl.py          # YAML → dataclasses + validation
    schema_gen.py   # FieldSpec list → v2 schema envelope (pure)
    choices.py      # choice-record diff + upsert (+ fetch normalization)
    apply.py        # orchestration: category → choices → event types;
                    # diffing incl. v2 GET-schema normalization; dry-run
    events.py       # post-event payload building + batch posting
```

`schema_gen.py` and `dsl.py` are pure (no I/O) — the bulk of unit
testing lands there. `apply.py` takes the client as a parameter
(injectable for tests).

## Error handling

- Spec validation errors: collected and printed together with YAML-ish
  paths (`event_types[0].fields[2].type: unknown type 'selects'`),
  exit 1, no API calls made.
- Auth failure: clear message ("login failed for <user> at <server>"),
  exit 1.
- API errors mid-apply: name the failing object ("creating event type
  'animal_sighting': 400 ... <server detail>"), stop (no attempt to
  continue past a failed dependency), exit 1. Objects already written
  stay written — re-running `apply` is the recovery path (idempotent).

## Testing

- `tests/test_dsl.py` — parsing, shorthand expansion, every validation
  rule.
- `tests/test_schema_gen.py` — per-type json/ui mapping, envelope shape,
  required, ordering, choice-field naming + truncation.
- `tests/test_choices.py` — upsert decisions (create / patch /
  deactivate / reactivate / no-op) against canned existing records.
- `tests/test_apply.py` — full apply against a `Mock` ERClient with
  canned GET responses: fresh site, no-change re-apply, edited spec
  (display change, added field, removed option), dry-run performs no
  writes; GET-schema normalization (additionalProperties →
  unevaluatedProperties) yields "unchanged".
- `tests/test_events.py` — flag coercion, payload shape, batch file,
  partial-failure exit code.

## Docs & examples

- `README.md` — install, auth setup, command reference, and a short
  walkthrough: write a spec → dry-run → apply → post an event → see it
  in ER.
- `examples/wildlife_monitoring.yaml` — a commented sample spec
  exercising every field type.
- `examples/events.yaml` — a sample batch-events file.
