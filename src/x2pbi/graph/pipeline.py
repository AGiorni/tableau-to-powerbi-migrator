"""Orchestration: run the migration stages either directly or via a LangGraph graph.

``run_migration`` executes the fixed stage sequence and is the simplest entry point.
``build_graph`` wires the same node functions into a LangGraph ``StateGraph`` for callers that
want streaming, checkpointing, or to insert conditional repair edges later.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from x2pbi.graph.state import STAGES, PipelineState
from x2pbi.ir.powerbi_ir import PBIModel
from x2pbi.mapping.report import MigrationReport


@dataclass
class MigrationResult:
    """Outcome of a migration run."""

    pbip_path: Path
    model: PBIModel
    report: MigrationReport


def run_migration(
    input_path: str,
    out_dir: str,
    chat_model: Any | None = None,
    preflight: bool = False,
    source: str | None = None,
) -> MigrationResult:
    """Run all pipeline stages in order and return the result.

    ``source`` names the adapter to use (``"tableau"``, ``"spotfire"``); when omitted it is
    inferred from the input file's extension.
    """

    state = PipelineState(
        input_path=input_path,
        out_dir=out_dir,
        chat_model=chat_model,
        preflight=preflight,
        source=source,
    )
    for stage in STAGES:
        state = stage(state)

    assert state.pbip_path is not None and state.model is not None
    return MigrationResult(
        pbip_path=Path(state.pbip_path),
        model=state.model,
        report=state.report,
    )


def build_graph():
    """Build a LangGraph ``StateGraph`` over the migration stages.

    Returns the compiled graph. Raises ``ImportError`` if LangGraph is not installed.
    """

    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(PipelineState)
    node_names = []
    for stage in STAGES:
        name = stage.__name__
        graph.add_node(name, stage)
        node_names.append(name)

    graph.add_edge(START, node_names[0])
    for earlier, later in pairwise(node_names):
        graph.add_edge(earlier, later)
    graph.add_edge(node_names[-1], END)

    return graph.compile()
