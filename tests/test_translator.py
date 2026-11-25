"""Tests for Tableau calc → DAX translation.

The helpers are tested directly; translation end to end runs through the migration graph. The
deterministic tests use a fake chat model so they run with no network or credentials. The live
test is skipped unless Azure OpenAI is configured via environment variables.
"""

from __future__ import annotations

import pytest

from t2pbi.config import load_azure_openai_config
from t2pbi.graph import MigrationContext, migrate
from t2pbi.llm import build_chat_model
from t2pbi.mapping.expression_translator import (
    EMPTY_RESPONSE_FEEDBACK,
    check_dax,
    clean_dax,
    translate_calc,
)
from t2pbi.mapping.model_mapper import UNTRANSLATED_DAX
from t2pbi.mapping.report import IssueSeverity


def _by_formula(answers: dict[str, str]):
    """A chat reply that returns canned DAX for the first formula substring found, else BLANK()."""

    return lambda prompt: next((dax for needle, dax in answers.items() if needle in prompt), "BLANK()")


def _migrate(twb, out_dir, chat):
    return migrate(twb, out_dir, MigrationContext(chat_model=chat, run_pbir_validation=False))


def _expression_issues(result):
    return [i for i in result.report.issues if i.stage == "expression"]


def test_translate_updates_measures(superstore_twb, tmp_path, fake_chat):
    fake = fake_chat(
        _by_formula({
            "SUM([Profit]) / SUM([Sales])": "DIVIDE(SUM('Superstore'[Profit]), SUM('Superstore'[Sales]))",
            "{ FIXED [Customer Name]": "AVERAGEX(VALUES('Superstore'[Customer Name]), CALCULATE(SUM('Superstore'[Sales])))",
        })
    )

    result = _migrate(superstore_twb, tmp_path, fake)

    profit_ratio = next(m for m in result.model.tables[0].measures if m.name == "Profit Ratio")
    assert profit_ratio.expression == "DIVIDE(SUM('Superstore'[Profit]), SUM('Superstore'[Sales]))"
    # Successful translation clears the TODO description.
    assert profit_ratio.description is None
    # One call per field: both were accepted on the first pass.
    assert len(fake.prompts) == 2
    assert any(i.severity is IssueSeverity.INFO for i in _expression_issues(result))


def test_no_model_leaves_placeholders_and_warns(superstore_twb, tmp_path, mapped):
    result = _migrate(superstore_twb, tmp_path, None)

    for measure in result.model.tables[0].measures:
        assert measure.expression == UNTRANSLATED_DAX
    # Without an LLM, every pending field is a warning (awaiting translation), not an error.
    issues = _expression_issues(result)
    assert len(issues) == len(mapped(superstore_twb).pending_expressions)
    assert all(i.severity is IssueSeverity.WARNING for i in issues)


def test_blank_response_flags_error(superstore_twb, tmp_path, mapped, fake_chat):
    fake = fake_chat("BLANK()")

    result = _migrate(superstore_twb, tmp_path, fake)

    for measure in result.model.tables[0].measures:
        assert measure.expression == UNTRANSLATED_DAX
    issues = _expression_issues(result)
    assert all(i.severity is IssueSeverity.ERROR for i in issues)
    assert len(issues) == len(mapped(superstore_twb).pending_expressions)


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


def test_check_dax(superstore_twb, mapped):
    model = mapped(superstore_twb).model

    assert check_dax(None, model).rejections == [EMPTY_RESPONSE_FEEDBACK]
    assert check_dax(UNTRANSLATED_DAX, model).rejections == [EMPTY_RESPONSE_FEEDBACK]
    assert not check_dax("SUM('Superstore'[Sales]", model).accepted

    ok = check_dax("SUM('Superstore'[Sales])", model)
    assert ok.accepted and not ok.reference_warnings

    # Unknown references are a warning, never a rejection.
    unknown = check_dax("SUM('Superstore'[Profit])", model)
    assert unknown.accepted and unknown.reference_warnings


def test_translate_calc_degrades_on_provider_error(superstore_twb, mapped, fake_chat):
    calc = next(iter(mapped(superstore_twb).pending_expressions.values()))
    assert translate_calc(calc, "Superstore", "", fake_chat(RuntimeError("provider down"))) is None


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
