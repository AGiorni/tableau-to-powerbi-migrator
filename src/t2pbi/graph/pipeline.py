"""The migration graph: the one and only way a migration runs.

[route] marks a conditional edge and the label its router returns::

    START
      -> ingest
      -> map_model
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

from dataclasses import dataclass
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from t2pbi.desktop import bridge
from t2pbi.graph import nodes
from t2pbi.graph.state import MigrationContext, MigrationState
from t2pbi.ir.powerbi_ir import PBIModel
from t2pbi.mapping.report import MigrationReport


@dataclass
class MigrationResult:
    """Typed view of the graph's final state, for callers that just want the outcome."""

    pbip_path: Path
    model: PBIModel
    report: MigrationReport


MigrationGraph = CompiledStateGraph[MigrationState, MigrationContext, MigrationState, MigrationState]


def build_graph() -> MigrationGraph:
    """Build and compile the migration graph."""

    graph = StateGraph(MigrationState, context_schema=MigrationContext)

    graph.add_node("ingest", nodes.ingest)
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
    graph.add_edge("ingest", "map_model")
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
    )
