"""Tests for the LLM-backed visual router (offline, using a fake chat model)."""

from __future__ import annotations

from x2pbi.ir.powerbi_ir import FieldKind, PBIFieldRef
from x2pbi.mapping.visual_decision import Ambiguity, DecisionSource, MappingDecision
from x2pbi.mapping.visual_router_llm import LLMVisualRouter


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChat:
    """Returns a canned string, or raises, to exercise the router without a provider."""

    def __init__(self, content: str | None = None, raises: bool = False) -> None:
        self._content = content
        self._raises = raises

    def invoke(self, _messages):
        if self._raises:
            raise RuntimeError("provider down")
        return _Resp(self._content)


def _ambiguous_decision() -> MappingDecision:
    return MappingDecision(
        visual_type="areaChart",
        wells={
            "Category": [PBIFieldRef(entity="Orders", property="Order Date")],
            "Y": [PBIFieldRef(entity="Orders", property="Sales", kind=FieldKind.AGGREGATION, function=0)],
            "Rows": [PBIFieldRef(entity="Orders", property="Segment")],
        },
        ambiguity=Ambiguity(reasons=["trellis vs legend"], candidates=["areaChart"]),
    )


def test_llm_router_applies_chosen_roles():
    chat = _FakeChat(
        '{"visual_type":"areaChart","roles":{"Category":["Order Date"],"Y":["Sales"],'
        '"Series":["Segment"]},"rationale":"legend","confidence":0.8}'
    )
    resolved = LLMVisualRouter(chat).resolve(_ambiguous_decision())

    assert resolved.source is DecisionSource.LLM
    assert resolved.confidence == 0.8
    assert {f.property for f in resolved.wells["Series"]} == {"Segment"}
    assert "Rows" not in resolved.wells


def test_llm_router_falls_back_on_bad_json():
    resolved = LLMVisualRouter(_FakeChat("sorry, not JSON")).resolve(_ambiguous_decision())

    assert resolved.source is DecisionSource.DEFAULT
    assert resolved.confidence <= 0.5
    # Deterministic guess preserved.
    assert {f.property for f in resolved.wells["Rows"]} == {"Segment"}


def test_llm_router_falls_back_on_provider_error():
    resolved = LLMVisualRouter(_FakeChat(raises=True)).resolve(_ambiguous_decision())

    assert resolved.source is DecisionSource.DEFAULT


def test_llm_router_rejects_answer_missing_required_role():
    # Drops required "Y" -> answer is worse than the guess, so we keep the deterministic one.
    chat = _FakeChat('{"visual_type":"areaChart","roles":{"Category":["Order Date"]}}')
    resolved = LLMVisualRouter(chat).resolve(_ambiguous_decision())

    assert resolved.source is DecisionSource.DEFAULT
    assert "Y" in resolved.wells


def test_llm_router_ignores_non_ambiguous_decision():
    decision = MappingDecision(
        visual_type="tableEx",
        wells={"Values": [PBIFieldRef(entity="Orders", property="City")]},
    )
    resolved = LLMVisualRouter(_FakeChat("{}")).resolve(decision)

    assert resolved.source is DecisionSource.RULES


def test_llm_router_resolves_unmatched_sheet_from_available_fields():
    decision = MappingDecision(
        visual_type="",
        ambiguity=Ambiguity(
            reasons=["no deterministic rule matched"],
            candidates=["pieChart", "tableEx"],
            available_fields=[
                PBIFieldRef(entity="Orders", property="Category"),
                PBIFieldRef(entity="Orders", property="Sales", kind=FieldKind.AGGREGATION, function=0),
            ],
        ),
    )
    chat = _FakeChat(
        '{"visual_type":"pieChart","roles":{"Category":["Category"],"Y":["Sales"]},"confidence":0.7}'
    )
    resolved = LLMVisualRouter(chat).resolve(decision)

    assert resolved.source is DecisionSource.LLM
    assert resolved.visual_type == "pieChart"
    assert {f.property for f in resolved.wells["Category"]} == {"Category"}
