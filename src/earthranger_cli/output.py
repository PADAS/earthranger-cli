"""Uniform read output: {"records": [...], "meta": {...}} to stdout or a file.

This is er-cli's (and the Skylight CLI's) shape, so agent skills can
"write to -o PATH, then read the file" identically across tools.
"""

from __future__ import annotations

import json
from pathlib import Path

import click


def emit(records: list, meta: dict, output: str | None) -> None:
    doc = {"records": records, "meta": meta}
    text = json.dumps(doc, indent=2, default=str)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n")
        click.echo(
            f"Done. {meta.get('total', len(records))} record(s) written to {output} "
            f"({meta.get('pages', 1)} page(s)).",
            err=True,
        )
    else:
        click.echo(text)
