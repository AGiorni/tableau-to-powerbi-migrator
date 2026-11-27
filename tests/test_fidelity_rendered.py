"""Numeric and visual fidelity: everything that doesn't need Power BI Desktop or a real model.

The Desktop-backed run itself (`scripts/fidelity.py --render`) is checked by hand; here the query
and the judge are fakes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from t2pbi.desktop.modeling import parse_csv_rows
from t2pbi.fidelity import load_expectations, read_project
from t2pbi.fidelity.expectations import Reference, _parse_sheet
from t2pbi.fidelity.numeric import dax_for, score_numeric
from t2pbi.fidelity.visual import VisualVerdict, judge_sheet, parse_verdict
from t2pbi.graph import MigrationContext, migrate

_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("text", "value", "tolerance"),
    [("29", 29.0, 0.5), ("10,194", 10194.0, 0.5), ("$2,326,534.35", 2326534.35, 0.005), ("15.54%", 0.1554, 0.00005)],
)
def test_reference_precision_follows_what_tableau_displayed(text, value, tolerance):
    ref = Reference.parse(text)
    assert ref.value == pytest.approx(value)
    assert ref.tolerance == pytest.approx(tolerance)


def test_reference_accepts_within_display_rounding():
    assert Reference.parse("29").accepts(28.67)
    assert not Reference.parse("29").accepts(27.9)
    assert Reference.parse("15.54%").accepts(0.155389)


def test_bad_reference_value_is_rejected_at_load():
    with pytest.raises(ValueError, match="not a number"):
        _parse_sheet({"name": "s", "values": {"Sales:sum": "lots"}})


def test_dax_for_bound_fields():
    col = {"Column": {"Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": "Sales"}}
    assert dax_for({"Aggregation": {"Expression": col, "Function": 0}}) == "SUM('Orders'[Sales])"
    assert dax_for({"Aggregation": {"Expression": col, "Function": 1}}) == "AVERAGE('Orders'[Sales])"
    measure = {"Measure": {"Expression": {"SourceRef": {"Entity": "O'Neil"}}, "Property": "a]b"}}
    assert dax_for(measure) == "'O''Neil'[a]]b]"
    assert dax_for(col) is None  # a plain column isn't a value


def test_parse_csv_rows_strips_bracketed_headers():
    assert parse_csv_rows("[v0],[v1]\r\n10194,\r\n") == [{"v0": "10194", "v1": ""}]
    assert parse_csv_rows("") == []


def test_numeric_scoring_on_book1_with_a_fake_engine(tmp_path):
    expected = load_expectations(_ROOT / "samples" / "Book1" / "expected.toml")
    migrate(_ROOT / "samples" / "Book1" / "Book1.twb", tmp_path, MigrationContext(run_output_validation=False))
    project = read_project(tmp_path)
    seen = []

    async def fake_query(dax):
        seen.append(dax)
        # What the real engine returned for the no-LLM output: plain aggregations and the row-count
        # measures have values, the untranslated measures are BLANK.
        values = {
            "AVERAGE('Orders'[Discount])": "0.155389", "SUM('Orders'[Profit])": "292296.8115",
            "SUM('Orders'[Quantity])": "38654", "SUM('Orders'[Sales])": "2326534.354",
            "'Orders'[Orders (Count)]": "10194", "'Returns'[Returns (Count)]": "296",
        }
        row = {}
        for part in dax.removeprefix("EVALUATE ROW(").removesuffix(")").split(', "'):
            name, expr = part.strip('"').split('", ', 1)
            row[name] = values.get(expr, "")
        return [row]

    results = asyncio.run(score_numeric(expected, project, fake_query))

    assert len(seen) == 1  # one query for the KPI sheet
    outcomes = {r.check: r.outcome for r in results}
    assert outcomes["value[Sales:sum]"] == "pass"
    assert outcomes["value[Discount:avg]"] == "pass"
    assert outcomes["value[Orders:rowcount]"] == "pass"  # the COUNTROWS measure the first pass adds
    assert outcomes["value[profit ratio:measure]"] == "gap"  # BLANK without the LLM
    assert not [r for r in results if r.outcome in ("fail", "fixed")]


def test_verdict_parsing_keeps_only_rubric_criteria_and_clamps_score():
    answer = (
        'Sure: {"criteria": {"chart_type": {"match": true, "note": "area"}, '
        '"colour": {"match": false, "note": "no split"}, "made_up": {"match": true}}, '
        '"score": 14, "summary": "close"}'
    )
    verdict = parse_verdict(VisualVerdict(sheet="s", reference="r.png"), answer)
    assert verdict.score == 10
    assert set(verdict.criteria) == {"chart_type", "colour"}
    assert verdict.mismatches == ["colour"]
    assert parse_verdict(VisualVerdict(sheet="s", reference="r"), "no json").error


def test_judge_sends_every_view_and_survives_provider_errors(tmp_path, fake_chat):
    png, jpg = tmp_path / "view.png", tmp_path / "legend.jpg"
    png.write_bytes(b"\x89PNG fake")
    jpg.write_bytes(b"\xff\xd8 fake")
    sheet = _parse_sheet({"name": "Sales by Category"})

    ok = fake_chat('{"criteria": {"layout": {"match": true, "note": ""}}, "score": 8, "summary": "ok"}')
    verdict = judge_sheet(ok, sheet, [png], png)
    assert verdict.score == 8 and verdict.error is None and verdict.reference == "view.png"

    judge_sheet(ok, sheet, [jpg, png], png)
    content = ok.prompts[-1]
    urls = [block["image_url"]["url"] for block in content if block["type"] == "image_url"]
    assert [url.split(";", 1)[0] for url in urls] == ["data:image/jpeg", "data:image/png", "data:image/png"]
    labels = [block["text"] for block in content if block["type"] == "text"]
    assert "Image 1: the Tableau original (view 1 of 2: legend.jpg)." in labels
    assert labels[-1] == "Image 3: the migrated Power BI page."

    failing = judge_sheet(fake_chat(RuntimeError("quota")), sheet, [png], png)
    assert failing.error == "RuntimeError: quota"
