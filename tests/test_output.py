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
