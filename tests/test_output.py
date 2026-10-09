import json

from earthranger_cli.output import emit


def test_emit_stdout_is_pretty_json_document(capsys):
    emit([{"id": "a"}], {"total": 1, "pages": 1}, None)
    out, err = capsys.readouterr()
    assert err == ""
    assert json.loads(out) == {"records": [{"id": "a"}], "meta": {"total": 1, "pages": 1}}
    assert out.startswith('{\n  "records"')  # indent=2
    assert out.endswith("\n")


def test_emit_file_writes_json_and_one_stderr_line(tmp_path, capsys):
    target = tmp_path / "nested" / "dir" / "out.json"
    emit([{"id": "a"}, {"id": "b"}], {"total": 2, "pages": 3}, str(target))
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"Done. 2 record(s) written to {target} (3 page(s)).\n"
    assert json.loads(target.read_text())["meta"]["pages"] == 3
    assert target.read_text().endswith("\n")


def test_emit_serializes_non_json_values_with_str(capsys):
    from datetime import UTC, datetime

    emit([{"when": datetime(2026, 9, 23, tzinfo=UTC)}], {"total": 1, "pages": 1}, None)
    out, _ = capsys.readouterr()
    assert json.loads(out)["records"][0]["when"] == "2026-09-23 00:00:00+00:00"


import click
import pytest

from earthranger_cli.output import cell, check_format, parse_fields, pluck, project, render


def test_parse_fields_splits_and_strips():
    assert parse_fields("id, name ,reported_by.name") == ["id", "name", "reported_by.name"]
    assert parse_fields(None) is None
    assert parse_fields("") is None


def test_pluck_follows_dotted_paths_and_list_indexes():
    rec = {"id": "a", "last_position": {"geometry": {"coordinates": [36.8, -1.3]}}, "tags": ["x"]}
    assert pluck(rec, "id") == "a"
    assert pluck(rec, "last_position.geometry.coordinates.1") == -1.3
    assert pluck(rec, "last_position.geometry.coordinates.-1") == -1.3
    assert pluck(rec, "missing.deep") is None
    # Review Focus 4: out-of-range index and a name where a list needs an index
    assert pluck(rec, "last_position.geometry.coordinates.5") is None
    assert pluck(rec, "tags.name") is None
    assert pluck(rec, "id.anything") is None


def test_project_keeps_only_requested_fields():
    recs = [{"id": "a", "n": 1, "x": {"y": 2}}, {"id": "b"}]
    assert project(recs, ["id", "x.y"]) == [{"id": "a", "x.y": 2}, {"id": "b", "x.y": None}]
    assert project(recs, None) is recs


def test_cell_renders_nested_values_as_compact_json():
    assert cell(None) == ""
    assert cell(True) == "true"
    assert cell({"a": [1, 2]}) == '{"a":[1,2]}'
    assert cell(3) == "3"


def test_render_tsv_escapes_tabs_and_newlines():
    recs = [{"id": "a", "title": "tab\there\nnew"}]
    out = render(recs, {}, ["id", "title"], "tsv")
    assert out == "id\ttitle\na\ttab\\there\\nnew\n"


def test_render_csv_quotes_commas():
    recs = [{"id": "a", "title": "x, y"}]
    assert render(recs, {}, ["id", "title"], "csv") == 'id,title\na,"x, y"\n'


def test_render_json_projects_records_and_keeps_meta():
    out = json.loads(render([{"id": "a", "n": 1}], {"total": 1, "pages": 1}, ["id"], "json"))
    assert out == {"records": [{"id": "a"}], "meta": {"total": 1, "pages": 1}}


def test_check_format_requires_fields_for_tables():
    check_format(None, "json")
    check_format(["id"], "tsv")
    with pytest.raises(click.UsageError, match="--format tsv needs --fields"):
        check_format(None, "tsv")


def test_emit_tsv_to_file_keeps_done_line_on_stderr(tmp_path, capsys):
    target = tmp_path / "t.tsv"
    emit([{"id": "a"}], {"total": 1, "pages": 1}, str(target), fields=["id"], fmt="tsv")
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"Done. 1 record(s) written to {target} (1 page(s)).\n"
    assert target.read_text() == "id\na\n"


def test_render_tsv_escapes_backslashes_so_escaped_tabs_are_unambiguous():
    out = render([{"p": "C:\\temp\\new", "q": "a\tb"}], {}, ["p", "q"], "tsv")
    assert out == "p\tq\nC:\\\\temp\\\\new\ta\\tb\n"


def test_emit_writes_utf8_regardless_of_locale(tmp_path, monkeypatch):
    import pathlib

    seen = {}
    real = pathlib.Path.write_text

    def spy(self, data, *args, **kwargs):
        seen["encoding"] = kwargs.get("encoding")
        return real(self, data, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", spy)
    emit([{"t": "Nyumbu – José 🐘"}], {"total": 1, "pages": 1}, str(tmp_path / "o.json"))
    assert seen["encoding"] == "utf-8"
    doc = json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))
    assert doc["records"][0]["t"] == "Nyumbu – José 🐘"


def test_table_formats_echo_meta_notes_to_stderr(capsys, tmp_path):
    meta = {"total": 1, "pages": 1, "truncated": True, "note": "stopped after 200 page(s)"}
    emit([{"id": "a"}], meta, None, fields=["id"], fmt="tsv")
    out, err = capsys.readouterr()
    assert out == "id\na\n"
    assert err == "note: stopped after 200 page(s)\n"
    emit([{"id": "a"}], meta, str(tmp_path / "t.csv"), fields=["id"], fmt="csv")
    _, err = capsys.readouterr()
    assert "note: stopped after 200 page(s)" in err and "Done. 1 record(s)" in err
    emit([{"id": "a"}], meta, None, fields=["id"], fmt="json")
    _, err = capsys.readouterr()
    assert err == ""  # JSON carries meta itself
