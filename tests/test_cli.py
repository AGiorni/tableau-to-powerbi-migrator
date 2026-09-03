"""Tests for the CLI's source selection and its error exit codes."""

from __future__ import annotations

from x2pbi.cli import main


def test_sources_lists_every_adapter(capsys):
    assert main(["sources"]) == 0

    out = capsys.readouterr().out
    assert "tableau" in out and ".twb" in out
    assert "spotfire" in out and ".dxp" in out


def test_migrate_infers_the_adapter_from_the_extension(superstore_twb, tmp_path):
    assert main(["migrate", str(superstore_twb), "--out", str(tmp_path), "--no-llm",
                 "--no-validate"]) == 0
    assert (tmp_path / "superstore.pbip").exists()


def test_unknown_extension_exits_two_without_a_traceback(tmp_path, capsys):
    report = tmp_path / "report.pbix"
    report.write_bytes(b"")

    assert main(["migrate", str(report), "--out", str(tmp_path), "--no-llm", "--no-validate"]) == 2
    assert "No source adapter handles" in capsys.readouterr().err


def test_wrong_source_for_the_file_exits_two(superstore_twb, tmp_path, capsys):
    code = main(["migrate", str(superstore_twb), "--source", "spotfire", "--out", str(tmp_path),
                 "--no-llm", "--no-validate"])

    assert code == 2
    assert "Expected .dxp" in capsys.readouterr().err


def test_unimplemented_adapter_exits_three(tmp_path, capsys):
    # Distinct from exit 2 so a caller can tell "not supported yet" from "you typed it wrong".
    analysis = tmp_path / "analysis.dxp"
    analysis.write_bytes(b"not a real dxp")

    code = main(["migrate", str(analysis), "--out", str(tmp_path), "--no-llm", "--no-validate"])

    assert code == 3
    assert "not implemented" in capsys.readouterr().err
