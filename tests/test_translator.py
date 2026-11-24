"""Tests for Tableau calc → DAX translation.

The helpers are tested directly; translation end to end runs through the migration graph. The
deterministic tests use a fake chat model so they run with no network or credentials. The live
test is skipped unless Azure OpenAI is configured via environment variables.
"""

from __future__ import annotations

import pytest

from t2pbi.config import load_azure_openai_config
from t2pbi.graph import MigrationContext, migrate
from t2pbi.ingest import load_workbook
from t2pbi.llm import build_chat_model
from t2pbi.mapping.expression_translator import (
    EMPTY_RESPONSE_FEEDBACK,
    check_dax,
    clean_dax,
    translate_calc,
)
from t2pbi.mapping.model_mapper import UNTRANSLATED_DAX, map_model
from t2pbi.mapping.report import IssueSeverity
from t2pbi.parse import parse_workbook


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChat:
    """Returns canned DAX based on a substring of the incoming Tableau formula."""

    def __init__(self, by_formula: dict[str, str]) -> None:
        self.by_formula = by_formula
        self.prompts: list[str] = []

    def invoke(self, messages):
        human = messages[-1].content
        self.prompts.append(human)
        for needle, dax in self.by_formula.items():
            if needle in human:
                return _FakeResponse(dax)
        return _FakeResponse("BLANK()")


def _migrate(twb, out_dir, chat):
    return migrate(twb, out_dir, MigrationContext(chat_model=chat, run_pbir_validation=False))


def _expression_issues(result):
    return [i for i in result.report.issues if i.stage == "expression"]


def _pending_count(twb) -> int:
    return len(map_model(parse_workbook(load_workbook(twb))).pending_expressions)


def test_translate_updates_measures(superstore_twb, tmp_path):
    fake = _FakeChat(
        {
            "SUM([Profit]) / SUM([Sales])": "DIVIDE(SUM('Superstore'[Profit]), SUM('Superstore'[Sales]))",
            "{ FIXED [Customer Name]": "AVERAGEX(VALUES('Superstore'[Customer Name]), CALCULATE(SUM('Superstore'[Sales])))",
        }
    )

    result = _migrate(superstore_twb, tmp_path, fake)

    profit_ratio = next(m for m in result.model.tables[0].measures if m.name == "Profit Ratio")
    assert profit_ratio.expression == "DIVIDE(SUM('Superstore'[Profit]), SUM('Superstore'[Sales]))"
    # Successful translation clears the TODO description.
    assert profit_ratio.description is None
    # One call per field: both were accepted on the first pass.
    assert len(fake.prompts) == 2
    assert any(i.severity is IssueSeverity.INFO for i in _expression_issues(result))


def test_no_model_leaves_placeholders_and_warns(superstore_twb, tmp_path):
    result = _migrate(superstore_twb, tmp_path, None)

    for measure in result.model.tables[0].measures:
        assert measure.expression == UNTRANSLATED_DAX
    # Without an LLM, every pending field is a warning (awaiting translation), not an error.
    issues = _expression_issues(result)
    assert len(issues) == _pending_count(superstore_twb)
    assert all(i.severity is IssueSeverity.WARNING for i in issues)


def test_blank_response_flags_error(superstore_twb, tmp_path):
    fake = _FakeChat({})  # everything falls through to BLANK()

    result = _migrate(superstore_twb, tmp_path, fake)

    for measure in result.model.tables[0].measures:
        assert measure.expression == UNTRANSLATED_DAX
    issues = _expression_issues(result)
    assert all(i.severity is IssueSeverity.ERROR for i in issues)
    assert len(issues) == _pending_count(superstore_twb)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SUM('T'[x])", "SUM('T'[x])"),
        ("```dax\nSUM('T'[x])\n```", "SUM('T'[x])"),
        ("```\nSUM('T'[x])\n```", "SUM('T'[x])"),
        ("Profit Ratio = DIVIDE([a],[b])", "DIVIDE([a],[b])"),
        ("'My Measure' = SUM('T'[x])", "SUM('T'[x])"),
        ("  SUM('T'[x])  ", "SUM('T'[x])"),
        # A boolean-returning expression must NOT have its leading token stripped.
        ("[a] = [b]", "[a] = [b]"),
        (None, None),
        ("", None),
    ],
)
def test_clean_dax(raw, expected):
    assert clean_dax(raw) == expected


def test_check_dax(superstore_twb):
    model = map_model(parse_workbook(load_workbook(superstore_twb))).model

    assert check_dax(None, model).rejections == [EMPTY_RESPONSE_FEEDBACK]
    assert check_dax(UNTRANSLATED_DAX, model).rejections == [EMPTY_RESPONSE_FEEDBACK]
    assert not check_dax("SUM('Superstore'[Sales]", model).accepted

    ok = check_dax("SUM('Superstore'[Sales])", model)
    assert ok.accepted and not ok.reference_warnings

    # Unknown references are a warning, never a rejection.
    unknown = check_dax("SUM('Superstore'[Profit])", model)
    assert unknown.accepted and unknown.reference_warnings


def test_translate_calc_degrades_on_provider_error(superstore_twb):
    class _Failing:
        def invoke(self, _messages):
            raise RuntimeError("provider down")

    result = map_model(parse_workbook(load_workbook(superstore_twb)))
    calc = next(iter(result.pending_expressions.values()))
    assert translate_calc(calc, "Superstore", "", _Failing()) is None


@pytest.mark.skipif(
    not load_azure_openai_config().is_configured,
    reason="Azure OpenAI not configured (set T2PBI_AZURE_OPENAI_ENDPOINT/DEPLOYMENT).",
)
def test_live_translation_smoke(superstore_twb, tmp_path):
    """Hits the real Azure OpenAI deployment when configured."""

    chat = build_chat_model()
    assert chat is not None

    result = _migrate(superstore_twb, tmp_path, chat)

    ratio = next(m for m in result.model.tables[0].measures if m.name == "Profit Ratio")
    # A plausible ratio translation should reference Sales and not be the placeholder.
    assert ratio.expression != UNTRANSLATED_DAX
    assert "Sales" in ratio.expression
