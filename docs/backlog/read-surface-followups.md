# Read surface follow-ups

Items noted during the PR #36 (otus parity) review rounds and deliberately
left out of that PR under the CONTRIBUTING stopping rule. Tracked as GitHub
issues so they can be picked up one at a time:

- **#38 — structure, not behaviour.** One `-o` writer shared by `emit` and
  the raw CSV export; one place deciding whether the site zone is needed
  (inside `windows.apply_window`, lazily). Also records the declined
  suggestion that `--fields`/`--format` imply `--json` on the authoring read
  commands (the spec's guardrail says the human output changes only under
  `--json` or `-o`).
- **#39 — low-priority ergonomics and considered-but-not-built behaviour.**
  Clearer messages (`er now` without a site zone, the default-window note,
  the `--group-by day` hint, the export `--state` kind); `--limit` with
  `--where` as a walk-until-N-matches mode; the spec's "Maybe" tier (`link`,
  `analyzers`, `patrols --with-distance`); nearest-match tuning; the DST
  midnight edge; validating enumerated flags in the README-examples test;
  shipping the pre-commit hook.

Design decisions these must respect live in
`docs/superpowers/specs/2026-09-02-er-cli-parity-design.md` (§P3 and its
guardrails) and in `CONTRIBUTING.md` §1.
