# Parked: import event-type JSON files into the DSL

Status: parked 2026-09-01 (design not finalized — blocked on seeing a sample
file). Captured from a working session so development can resume cold.

## The need

A user has compiled a collection of JSON files, one per event type. Each file
contains an Event Type object whose `schema` property is a **stringified**
version of the type's schema (the shape ER's own GET returns). They want to
bring these under spec management.

## Decided

- **Direction: convert to a DSL spec** (chosen over pushing the raw JSON to a
  site directly). A command shaped like:

  ```bash
  er events import one.json two.json ... -o spec.yaml
  ```

  Each file is inverted into the YAML DSL — reusing `pull.py`'s inversion
  (`_invert_event_type` and friends), which already handles stringified
  schemas (`normalize_v2_schema` json.loads's them), layout/sections,
  choices_field, extras, and the report-or-refuse contract for constructs the
  DSL can't express (`--skip-unsupported` semantics should match pull).

- The category for the generated spec: from the files' `category` values
  (error if they disagree), overridable with a flag.

## Open questions (ask before building)

1. **v1 or v2 schemas?** A stringified schema suggests either. If v1
   (`{"schema": ..., "definition": [...]}` with inline enums), the files are
   self-contained but the DSL has no v1 support — the import would need a
   v1→DSL inverter (inline enums → options; `definition` → field order/types).
   If v2 (`{json, ui}` envelope), the existing inversion applies directly.
2. **Where do select/multiselect options come from?** v2 schemas only
   *reference* Choice records (`$ref: …choices.json?field=X`):
   - resolve against a live site (`--server`/`--profile`) that has the records
   - bundled in the files (shape unknown — need the sample)
   - emit empty `options: []` + warnings for later hand-filling
   The session ended before the user answered; **get a sample file first** —
   it settles both questions at once.

## Groundwork already landed (all on the PR branch)

- `pull.py` inversion covers everything the DSL expresses; stringified and
  v1 schemas are detected (v1 → "not a v2 json/ui envelope" warning).
- Top-level `choices:` sets, option `icon` + `ordernum`, `choices_field`
  sharing — so converted output can represent shared/stock choice sets.
- Per-event-type `default_priority` / `default_state` / `readonly` — likely
  present in the user's files since they mirror GET output.
- `er choices list/show` for inspecting the destination site's sets.

## Sketch of remaining work

- `import` command in `cli.py` (events group): read files (each a single
  event-type object, or a list — accept both), parse/normalize schema, run
  the shared inversion, aggregate into one spec dict, hoist shared choice
  sets (reuse `_hoist_shared_sets`), render with `render_spec_yaml`.
- Refactor: `pull.pull_category` and the importer share everything below
  "fetch from server" — extract the inversion entry point so it takes an
  event-type dict plus a choices-resolver callable (live client vs bundled
  data vs empty).
- Tests mirror `tests/test_pull.py` patterns; round-trip guarantee where a
  choices source exists.
