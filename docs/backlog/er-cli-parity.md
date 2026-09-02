# er-cli parity

Bring tusker's `packages/er-cli` read surface (agent-facing `{records,
meta}` JSON output, bearer-token auth, read-only resource commands,
auto-pagination) into this repo so the two `er` binaries can be
consolidated. This repo stays an authoring tool; the additions are
read-only.

Full comparison, design decisions, open questions, and the prioritized
todo live in the spec:
`docs/superpowers/specs/2026-09-02-er-cli-parity-design.md`.
