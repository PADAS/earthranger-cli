"""Uniform read output: {"records": [...], "meta": {...}} to stdout or a file.

This is er-cli's (and the Skylight CLI's) shape, so agent skills can
"write to -o PATH, then read the file" identically across tools.

`--fields` and `--format` exist because the commonest thing done with the
JSON was piping it through jq for three columns: asking for the columns
keeps raw payloads out of an agent's context.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import click

FORMATS = ("json", "tsv", "csv")
# the backslash first, so an escaped tab can never be confused with a literal "\\t"
_TSV_ESCAPES = str.maketrans({"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"})


def parse_fields(raw: str | None) -> list[str] | None:
    """`"id,name,reported_by.name"` -> the paths, or None when not asked for."""
    if not raw:
        return None
    return [f.strip() for f in raw.split(",") if f.strip()] or None


def lookup(record, path: str) -> tuple[bool, object]:
    """Follow a dotted path into a record; a numeric segment indexes a list.

    Returns (found, value): `found` is whether the path exists at all, so a
    caller can tell a null value from a missing key in one walk. Missing is
    not an error: the point is extracting a column across records that do not
    all carry it, so a path that is not there is (False, None), including an
    index past either end of a list.
    """
    value = record
    for part in path.split("."):
        if isinstance(value, list):
            try:
                index = int(part)
            except ValueError:
                return False, None
            if not -len(value) <= index < len(value):
                return False, None
            value = value[index]
        elif isinstance(value, dict):
            if part not in value:
                return False, None
            value = value[part]
        else:
            return False, None
    return True, value


def pluck(record, path: str):
    """The value at a dotted path, or None when it is missing (an empty cell)."""
    return lookup(record, path)[1]


def project(records: list, fields: list[str] | None) -> list:
    if not fields:
        return records
    return [{f: pluck(r, f) for f in fields} for r in records]


def cell(value) -> str:
    """One value as text: None is empty, dicts and lists are compact JSON (not
    Python repr), booleans are lower-case so `grep true` works."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), default=str)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _tsv(records: list, fields: list[str]) -> str:
    # escape rather than quote: `cut -f2` and `awk -F'\t'` do not unquote
    rows = [fields] + [[cell(pluck(r, f)) for f in fields] for r in records]
    return "".join("\t".join(c.translate(_TSV_ESCAPES) for c in row) + "\n" for row in rows)


def _csv(records: list, fields: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(fields)
    for record in records:
        writer.writerow([cell(pluck(record, f)) for f in fields])
    return buf.getvalue()


def render(records: list, meta: dict, fields: list[str] | None, fmt: str) -> str:
    if fmt == "json":
        doc = {"records": project(records, fields), "meta": meta}
        return json.dumps(doc, indent=2, default=str) + "\n"
    return (_tsv if fmt == "tsv" else _csv)(records, fields or [])


def check_format(fields: list[str] | None, fmt: str) -> None:
    if fmt != "json" and not fields:
        raise click.UsageError(f"--format {fmt} needs --fields to say which columns to write.")


def emit(
    records: list,
    meta: dict,
    output: str | None,
    *,
    fields: list[str] | None = None,
    fmt: str = "json",
) -> None:
    text = render(records, meta, fields, fmt)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        click.echo(
            f"Done. {meta.get('total', len(records))} record(s) written to {output} "
            f"({meta.get('pages', 1)} page(s)).",
            err=True,
        )
    else:
        click.echo(text, nl=False)


def output_options(f):
    """`--fields` and `--format` for any command that emits the records document."""
    f = click.option(
        "--format",
        "fmt",
        type=click.Choice(FORMATS),
        default="json",
        show_default=True,
        help="json is the {records, meta} document; tsv/csv write a table of --fields.",
    )(f)
    f = click.option(
        "--fields",
        help="Comma-separated dotted paths to keep, "
        "e.g. id,name,last_position.geometry.coordinates.1.",
    )(f)
    return f
