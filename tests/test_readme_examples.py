"""Every `er ...` line in a README code block must parse against the real click
tree: options exist, subcommands exist, nothing is left over. This is what
would have caught the stale `profile use` wrapper docs (PR 34)."""

import re
import shlex
from pathlib import Path

import click
import pytest

from earthranger_cli.cli import main

README = Path(__file__).resolve().parents[1] / "README.md"
_FENCE = re.compile(r"```(?:bash|sh|zsh|console)?\n(.*?)```", re.DOTALL)


def example_lines() -> list[str]:
    lines = []
    for block in _FENCE.findall(README.read_text()):
        buf = ""
        for raw in block.splitlines():
            line = raw.strip()
            if buf:
                line = buf + " " + line
                buf = ""
            if line.endswith("\\"):
                buf = line[:-1].rstrip()
                continue
            if line.startswith("er "):
                lines.append(line)
    return lines


def _opts(cmd: click.Command) -> set[str]:
    return {opt for p in cmd.params for opt in (*p.opts, *p.secondary_opts)}


def parse(args: list[str]) -> None:
    """Resolve groups and parse options without running anything.

    Resilient parsing (no callbacks, no required-argument errors, so
    placeholders are fine) also swallows unknown options, so those are checked
    here: every `--flag` token must be declared somewhere on the command path.
    """
    ctx = main.make_context("er", list(args), resilient_parsing=True)
    cmd: click.Command = main
    known = _opts(main)
    while isinstance(cmd, click.Group):
        # click >= 8.2 keeps the first positional (the subcommand name) in a
        # private slot rather than ctx.args; older click exposes protected_args
        protected = getattr(ctx, "_protected_args", None)
        if protected is None:
            protected = getattr(ctx, "protected_args", [])
        rest = list(protected) + list(ctx.args)
        if not rest:
            return  # `er --help`-style: a group with nothing after it
        name, sub, rest = cmd.resolve_command(ctx, rest)
        if sub is None:
            raise AssertionError(f"no such command {rest[0] if rest else name!r}")
        ctx = sub.make_context(name, rest, parent=ctx, resilient_parsing=True)
        cmd = sub
        known |= _opts(sub)
    assert not ctx.args, f"unparsed arguments: {ctx.args}"
    for tok in args:
        if tok == "--":
            break
        if tok.startswith("--"):
            flag = tok.split("=", 1)[0]
            if flag not in known:
                raise click.NoSuchOption(flag)


def test_parse_rejects_a_bad_example():
    with pytest.raises((click.NoSuchOption, click.UsageError)):
        parse(["subjects", "search", "--bogus"])
    with pytest.raises(AssertionError):
        parse(["subjects", "frobnicate"])


def test_parse_accepts_a_good_example():
    parse(["--profile", "sandbox", "events", "list", "categories"])
    parse(["events", "search", "--event-type", "geofence_break", "-o", "/tmp/gf.json"])


@pytest.mark.parametrize("line", example_lines(), ids=lambda s: s[:60])
def test_readme_example_parses(line):
    args = shlex.split(line, comments=True)[1:]  # drop the leading `er`
    parse(args)
