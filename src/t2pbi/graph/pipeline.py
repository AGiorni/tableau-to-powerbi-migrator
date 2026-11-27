"""The migration graph: the one and only way a migration runs.

[route] marks a conditional edge and the label its router returns::

    START
      -> ingest
           [packaged_data]      -> unpack_extracts -> map_model
           [source_connections] -> map_model
           [translate]            -> translate_expressions -> validate_expressions
                                                                 [repair] -> translate_expressions
                                                                 [done]   -> map_visuals
           [no_llm]               -> flag_untranslated     -> map_visuals
           [nothing_to_translate] -> map_visuals
      -> map_visuals -> validate_model -> assemble
           [validate] -> validate_tmdl -> validate_pbir
                            [refine] -> prepare_workspace
                                          [desktop]     -> open_desktop -> assess
                                          [checks_only] -> assess
                                        assess
                                          [work] -> pick_item -> fix_item -> review_item
                                                                               [work] -> pick_item
                                                                               [done] -> finalize_refine
                                          [done] -> finalize_refine -> END
                            [done]   -> END
           [skip]     -> END

Refinement (``--refine``): ``assess`` builds the work list (one item per page); the loop gives
each page to the agent (``fix_item``), and ``review_item`` keeps its change as a git commit or
reverts it, with one retry per page. The agent proposes; deterministic nodes give it work and
decide what's kept.

New stages are new nodes; new decisions are new conditional edges. Keep loops and gates in the
graph, not inside nodes, so they stay visible (``build_graph().get_graph().draw_mermaid()``) and
checkpointable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from t2pbi.desktop import bridge
from t2pbi.graph import nodes
from t2pbi.graph.state import MigrationContext, MigrationState
from t2pbi.ir.powerbi_ir import PBIModel
from t2pbi.mapping.report import IssueSeverity, MigrationReport
from t2pbi.refine.review import VALIDATOR_CHECK

# Report stages of the validators, whose first-pass errors refinement re-measures.
_VALIDATOR_STAGES = {"tmdl", "pbir"}


@dataclass
class MigrationResult:
    """Typed view of the graph's final state, for callers that just want the outcome."""

    pbip_path: Path
    model: PBIModel
    report: MigrationReport
    # Errors still in the project when the run ended: ERROR issues, except that after refinement
    # the validators' first-pass errors are replaced by what they report on the final files (the
    # agent may have fixed some). Empty = a clean migration; the CLI exits 1 otherwise.
    errors: list[str] = field(default_factory=list)


MigrationGraph = CompiledStateGraph[MigrationState, MigrationContext, MigrationState, MigrationState]


def build_graph() -> MigrationGraph:
    """Build and compile the migration graph."""

    graph = StateGraph(MigrationState, context_schema=MigrationContext)

    graph.add_node("ingest", nodes.ingest)
    graph.add_node("unpack_extracts", nodes.unpack_extracts)
    graph.add_node("map_model", nodes.map_model)
    graph.add_node("translate_expressions", nodes.translate_expressions)
    graph.add_node("validate_expressions", nodes.validate_expressions)
    graph.add_node("flag_untranslated", nodes.flag_untranslated)
    graph.add_node("map_visuals", nodes.map_visuals)
    graph.add_node("validate_model", nodes.validate_model)
    graph.add_node("assemble", nodes.assemble)
    graph.add_node("validate_tmdl", nodes.validate_tmdl)
    graph.add_node("validate_pbir", nodes.validate_pbir)
    graph.add_node("prepare_workspace", nodes.prepare_workspace)
    graph.add_node("open_desktop", nodes.open_desktop)
    graph.add_node("assess", nodes.assess)
    graph.add_node("pick_item", nodes.pick_item)
    graph.add_node("fix_item", nodes.fix_item)
    graph.add_node("review_item", nodes.review_item)
    graph.add_node("finalize_refine", nodes.finalize_refine)

    graph.add_edge(START, "ingest")
    graph.add_conditional_edges(
        "ingest",
        nodes.route_packaged_data,
        {"packaged_data": "unpack_extracts", "source_connections": "map_model"},
    )
    graph.add_edge("unpack_extracts", "map_model")
    graph.add_conditional_edges(
        "map_model",
        nodes.route_expressions,
        {
            "translate": "translate_expressions",
            "no_llm": "flag_untranslated",
            "nothing_to_translate": "map_visuals",
        },
    )
    graph.add_edge("translate_expressions", "validate_expressions")
    graph.add_conditional_edges(
        "validate_expressions",
        nodes.route_after_expression_validation,
        {"repair": "translate_expressions", "done": "map_visuals"},
    )
    graph.add_edge("flag_untranslated", "map_visuals")
    graph.add_edge("map_visuals", "validate_model")
    graph.add_edge("validate_model", "assemble")
    graph.add_conditional_edges(
        "assemble",
        nodes.route_output_validation,
        {"validate": "validate_tmdl", "skip": END},
    )
    graph.add_edge("validate_tmdl", "validate_pbir")
    graph.add_conditional_edges(
        "validate_pbir",
        nodes.route_refine,
        {"refine": "prepare_workspace", "done": END},
    )
    graph.add_conditional_edges(
        "prepare_workspace",
        nodes.route_desktop,
        {"desktop": "open_desktop", "checks_only": "assess"},
    )
    graph.add_edge("open_desktop", "assess")
    work_routes = {"work": "pick_item", "done": "finalize_refine"}
    graph.add_conditional_edges("assess", nodes.route_work, work_routes)
    graph.add_edge("pick_item", "fix_item")
    graph.add_edge("fix_item", "review_item")
    graph.add_conditional_edges("review_item", nodes.route_work, work_routes)
    graph.add_edge("finalize_refine", END)

    return graph.compile()


def recursion_limit(context: MigrationContext) -> int:
    """Graph steps one run may take: the straight path, two per expression pass, three per agent run."""

    return 30 + 2 * context.max_translation_attempts + 3 * context.max_refine_items


def migrate(
    input_path: str | Path,
    out_dir: str | Path,
    context: MigrationContext | None = None,
) -> MigrationResult:
    """Invoke the migration graph on one workbook and return its typed result."""

    context = context or MigrationContext()
    try:
        final = build_graph().invoke(
            MigrationState(input_path=str(input_path), out_dir=str(out_dir)),
            {"recursion_limit": recursion_limit(context), "callbacks": list(context.callbacks)},
            context=context,
        )
    finally:
        bridge.close_all()  # a Power BI Desktop the refinement opened, if a node failed before closing it
    state = MigrationState.model_validate(final)
    assert state.pbip_path is not None and state.model is not None
    return MigrationResult(
        pbip_path=Path(state.pbip_path),
        model=state.model,
        report=MigrationReport(issues=state.issues),
        errors=_remaining_errors(state),
    )


def _remaining_errors(state: MigrationState) -> list[str]:
    refined = state.refine_baseline is not None
    errors = [
        f"{i.stage} {i.object_name}: {i.message}"
        for i in state.issues
        if i.severity is IssueSeverity.ERROR and not (refined and i.stage in _VALIDATOR_STAGES)
    ]
    if refined:  # failing_checks is current after every review: these are the final files' errors
        errors += [key for key in state.failing_checks if key.split("::", 1)[-1].startswith(VALIDATOR_CHECK)]
    return errors
