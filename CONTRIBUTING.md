# Contributing

How features move from idea to merged PR in this repo, and the rules we
adopted after the P3 read-surface work (PR #36) went through six review
rounds that a better process would have made two.

## 1. Settle the contract before writing code

The expensive review findings were definitions, not bugs: what a bare
date means on each endpoint, when the CLI may make an extra request,
what `exact` promises, how a truncated walk is reported. Write those
down in the spec (`docs/superpowers/specs/`) before implementation,
one short paragraph per contract, and get them agreed. A reviewer who
finds a gap in a definition will keep finding it until the definition
exists.

Checklist for any read-surface change:

- **Time.** Which zone does the server apply to a naive timestamp on
  this endpoint, and what do we send? (das: site zone inside the
  events/patrols `filter`; UTC for observation bounds; the export view
  rejects naive values outright.)
- **Requests.** Which commands may make a request beyond the data
  request, and why? The standing rule: the site clock (`GET /status`)
  is fetched only when a time flag, a period `--group-by`, `er now`, or
  a bare date on observations/tracks/exports needs it.
- **Completeness.** How does the output say the answer is partial
  (`meta.truncated`, `meta.exact`, `meta.unbucketed`, a `note`), and
  does every output format carry that signal?
- **Boundaries.** Page caps, default windows, and what refuses rather
  than widens (an export fallback with no selector, a listing whose
  `next` link loops).

## 2. One PR per feature

PR #36 bundled eight features across six modules. Every fix round
touched many files and every review had to hold all of it. Split along
the plan's own seams: a PR is one capability a reviewer can hold in
their head, with its own tests and README section.

## 3. Smoke-test against a real site once

The test suite runs against `FakeER`; it cannot know what das actually
does. Before asking for review, run each new flag once against the
sandbox (`er --profile sandbox ...`) and read the response. Several
rounds of #36 were about das behaviour the fake could not model: a
naive date compared to an aware one, a CSV served without a charset,
observation bounds read as UTC.

## 4. Review with a stopping rule

A medium-effort automated review returns a handful of findings every
time by construction, so "review, fix, review" never converges on its
own. The rule:

- After implementation, run **one high-effort review by a fresh
  reviewer** of the whole branch. Fix what is Critical or Important.
- Triage the rest by effect on a person using the CLI, not by the
  reviewer's label. Efficiency and structure notes go to a follow-up
  issue unless they are a one-line change in a file already being
  edited.
- Stop when a round yields **no correctness findings**, or after two
  rounds. Open an issue for what remains and merge.
- Push back on a finding that contradicts an agreed contract (for
  example, a fix that would add a request every read never pays for)
  rather than implementing it.

## 5. Let the PR watcher own review rounds

When a PR is being babysat (`/ce-babysit-pr`), request reviews through
GitHub so the loop handles comments, fixes, replies and resolutions in
one channel. Running ad-hoc reviews in parallel produces two streams of
findings on the same head.

## 6. Never commit on a hidden exit status

Twice in #36 a red test was pushed because `pytest ... | tail` reported
`tail`'s exit code. Chains that end in a commit must run the suite as
its own step with the real exit code, or under `set -o pipefail`. A
pre-commit hook that runs `uv run --extra dev pytest -q` makes the slip
impossible:

```bash
printf '#!/bin/sh\nuv run --extra dev pytest -q && uv run --extra dev ruff check src tests\n' > .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

## 7. Tests pin behaviour, docs pin examples

Every behaviour change lands with a test that failed first. Every `er …`
example in the README is parsed against the real command tree by
`tests/test_readme_examples.py`, so a stale flag in the docs fails the
suite; keep examples runnable, and note that the parser cannot check
argument *values* (an invalid `--state` still parses).

## What CI runs

Every pull request and push to main runs `.github/workflows/test.yml`: the
suite plus `ruff check` and `ruff format --check` on each Python the package
claims (3.11, 3.12, 3.13). A tag runs `release.yml`, which tests once more on
3.11, builds, and publishes. Adding a Python version means the classifiers in
`pyproject.toml` and the matrix in `test.yml` change together.

## Day-to-day commands

```bash
uv sync --extra dev                                   # one-time setup
uv run --extra dev pytest -q                          # the suite
uv run --extra dev ruff check src tests && uv run --extra dev ruff format --check src tests
uv run er --help                                      # the CLI from source
```

Commits are plain descriptive sentences with a body that explains why.
PR descriptions use `## What` and `## Verification`. Releases are cut by
a version-bump PR followed by a `vX.Y.Z` tag, which publishes to PyPI.
