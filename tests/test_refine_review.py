"""The review gate: the agent proposes, these rules decide (pure function, no files)."""

from __future__ import annotations

from t2pbi.refine.review import JUDGE_TOLERANCE, judge_verdict
from t2pbi.refine.worklist import Problem, ProblemSource, WorkItem

PAGE = "Sales by Category"
ITEM = WorkItem(page=PAGE, problems=[
    Problem(source=ProblemSource.DERIVED, check="fields[Profit:sum]", detail="missing"),
    Problem(source=ProblemSource.DERIVED, check="visual-type", detail="areaChart"),
])
BEFORE = {
    f"{PAGE}::fields[Profit:sum]": "missing",
    f"{PAGE}::visual-type": "areaChart",
    "Other page::title": "wrong title",
}


def test_a_change_that_fixes_a_target_and_breaks_nothing_is_kept():
    after = {k: v for k, v in BEFORE.items() if k != f"{PAGE}::fields[Profit:sum]"}
    verdict = judge_verdict(ITEM, BEFORE, after, [])
    assert verdict.accepted
    assert verdict.fixed == [f"{PAGE}::fields[Profit:sum]"]
    assert verdict.reasons == ["fixed: fields[Profit:sum]"]


def test_validator_errors_refuse_a_change():
    verdict = judge_verdict(ITEM, BEFORE, {}, ["PBIR schema: bad visual"])
    assert not verdict.accepted
    assert any("validators reject it" in r for r in verdict.reasons)


def test_breaking_a_passing_check_anywhere_refuses_a_change():
    after = {f"{PAGE}::visual-type": "areaChart", "Other page::title": "wrong title", "Map::sort": "lost"}
    verdict = judge_verdict(ITEM, BEFORE, after, [])
    assert not verdict.accepted
    assert any(r.startswith("now failing: Map::sort") for r in verdict.reasons)


def test_a_change_that_fixes_none_of_its_targets_is_refused():
    verdict = judge_verdict(ITEM, BEFORE, dict(BEFORE), [])
    assert not verdict.accepted
    assert "none of the page's failing checks passes now" in verdict.reasons


def test_a_judge_only_item_needs_the_score_to_rise():
    item = WorkItem(page=PAGE, problems=[Problem(source=ProblemSource.JUDGE, check="judge:colour", detail="no split")])
    assert not judge_verdict(item, BEFORE, dict(BEFORE), []).accepted  # no picture, no evidence
    assert not judge_verdict(item, BEFORE, dict(BEFORE), [], score_before=5, score_after=5).accepted
    kept = judge_verdict(item, BEFORE, dict(BEFORE), [], score_before=5, score_after=7)
    assert kept.accepted and kept.reasons == ["judge 5/10 -> 7/10"]


def test_the_judge_can_veto_a_change_only_beyond_its_noise():
    after = {k: v for k, v in BEFORE.items() if k != f"{PAGE}::visual-type"}
    within = judge_verdict(ITEM, BEFORE, after, [], score_before=6, score_after=6 - JUDGE_TOLERANCE)
    assert within.accepted
    worse = judge_verdict(ITEM, BEFORE, after, [], score_before=6, score_after=5 - JUDGE_TOLERANCE)
    assert not worse.accepted
    assert any("the picture got worse" in r for r in worse.reasons)
