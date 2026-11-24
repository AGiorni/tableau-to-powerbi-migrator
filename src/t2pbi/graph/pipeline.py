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
           [validate] -> validate_pbir -> END
           [skip]     -> END

New stages are new nodes; new decisions are new conditional edges. Keep loops and gates in the
graph, not inside nodes, so they stay visible (``build_graph().get_graph().draw_mermaid()``) and
checkpointable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

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
    graph.add_node("validate_pbir", nodes.validate_pbir)

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
        nodes.route_pbir_validation,
        {"validate": "validate_pbir", "skip": END},
    )
    graph.add_edge("validate_pbir", END)

    return graph.compile()


def migrate(
    input_path: str | Path,
    out_dir: str | Path,
    context: MigrationContext | None = None,
) -> MigrationResult:
    """Invoke the migration graph on one workbook and return its typed result."""

    final = build_graph().invoke(
        MigrationState(input_path=str(input_path), out_dir=str(out_dir)),
        context=context or MigrationContext(),
    )
    state = MigrationState.model_validate(final)
    assert state.pbip_path is not None and state.model is not None
    return MigrationResult(
        pbip_path=Path(state.pbip_path),
        model=state.model,
        report=MigrationReport(issues=state.issues),
    )
