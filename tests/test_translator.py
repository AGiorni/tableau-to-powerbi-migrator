"""Tests for the Tableau calc → DAX expression translator.

The deterministic tests use a fake chat model so they run with no network or credentials. The
live test is skipped unless Azure OpenAI is configured via environment variables.
"""

from __future__ import annotations

import pytest

from t2pbi.config import load_azure_openai_config
from t2pbi.ingest import load_workbook
from t2pbi.llm import build_chat_model
from t2pbi.mapping.expression_translator import _clean_dax, translate_expressions
from t2pbi.mapping.model_mapper import UNTRANSLATED_DAX, map_model
from t2pbi.mapping.report import IssueSeverity, MigrationReport
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


def _mapped(superstore_twb):
    result = map_model(parse_workbook(load_workbook(superstore_twb)))
    return result.model, result.pending_expressions


def test_translate_updates_measures(superstore_twb):
    model, pending = _mapped(superstore_twb)
    report = MigrationReport()
    fake = _FakeChat(
        {
            "SUM([Profit]) / SUM([Sales])": "DIVIDE(SUM('Superstore'[Profit]), SUM('Superstore'[Sales]))",
            "{ FIXED [Customer Name]": "AVERAGEX(VALUES('Superstore'[Customer Name]), CALCULATE(SUM('Superstore'[Sales])))",
        }
    )

    translate_expressions(model, pending, fake, report)

    profit_ratio = next(m for m in model.tables[0].measures if m.name == "Profit Ratio")
    assert profit_ratio.expression == "DIVIDE(SUM('Superstore'[Profit]), SUM('Superstore'[Sales]))"
    # Successful translation clears the TODO description.
    assert profit_ratio.description is None
    # Both calls were made.
    assert len(fake.prompts) == 2
    assert any(i.severity is IssueSeverity.INFO for i in report.issues)


def test_no_model_leaves_placeholders_and_warns(superstore_twb):
    model, pending = _mapped(superstore_twb)
    report = MigrationReport()

    translate_expressions(model, pending, None, report)

    for measure in model.tables[0].measures:
        assert measure.expression == UNTRANSLATED_DAX
    # Without an LLM, every pending field is a warning (awaiting translation), not an error.
    assert len(report.issues) == len(pending)
    assert all(i.severity is IssueSeverity.WARNING for i in report.issues)


def test_blank_response_flags_error(superstore_twb):
    model, pending = _mapped(superstore_twb)
    report = MigrationReport()
    fake = _FakeChat({})  # everything falls through to BLANK()

    translate_expressions(model, pending, fake, report)

    for measure in model.tables[0].measures:
        assert measure.expression == UNTRANSLATED_DAX
    assert all(i.severity is IssueSeverity.ERROR for i in report.issues)
    assert len(report.issues) == len(pending)


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
    assert _clean_dax(raw) == expected


@pytest.mark.skipif(
    not load_azure_openai_config().is_configured,
    reason="Azure OpenAI not configured (set T2PBI_AZURE_OPENAI_ENDPOINT/DEPLOYMENT).",
)
def test_live_translation_smoke(superstore_twb):
    """Hits the real Azure OpenAI deployment when configured."""

    chat = build_chat_model()
    assert chat is not None

    model, pending = _mapped(superstore_twb)
    report = MigrationReport()
    translate_expressions(model, pending, chat, report)

    ratio = next(m for m in model.tables[0].measures if m.name == "Profit Ratio")
    # A plausible ratio translation should reference Sales and not be the placeholder.
    assert ratio.expression != UNTRANSLATED_DAX
    assert "Sales" in ratio.expression
