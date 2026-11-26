"""What flows through the migration graph, and what the graph runs with.

Two deliberately separate things:

- :class:`MigrationState` is **data**: inputs, the intermediate representations each node
  produces, and the issues found along the way. Nodes read it and return partial updates; they
  never mutate it in place. Everything in it is a plain Pydantic value, so any step can be
  checkpointed, inspected, or replayed.
- :class:`MigrationContext` is **dependencies and settings** for one run: the chat model, which
  optional gates to run, retry budgets. It is injected into nodes and routers via LangGraph's
  ``Runtime`` and is never stored in the state.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, Field

from t2pbi.ir.powerbi_ir import PBIModel, PBIReportPage
from t2pbi.ir.tableau_ir import TableauCalculatedField, TableauWorkbook
from t2pbi.mapping.report import MigrationIssue
from t2pbi.refine.worklist import WorkItem


class MigrationState(BaseModel):
    """The graph state. Fields are listed in the order nodes produce them."""

    # Inputs.
    input_path: str
    out_dir: str

    # ingest
    workbook: TableauWorkbook | None = None

    # map_model; measures are refined by the expression loop.
    model: PBIModel | None = None

    # Expression loop. Calculated fields still without accepted DAX, keyed "<table>::<measure>".
    pending_expressions: dict[str, TableauCalculatedField] = Field(default_factory=dict)
    # Latest LLM answer per pending key (None = no usable answer); consumed by validation.
    dax_candidates: dict[str, str | None] = Field(default_factory=dict)
    # Why the model call itself failed on the latest pass (e.g. a 400 from the provider), per key.
    provider_errors: dict[str, str] = Field(default_factory=dict)
    # Why each pending key's last candidate was rejected; sent back to the LLM on repair.
    dax_feedback: dict[str, list[str]] = Field(default_factory=dict)
    # Completed translate passes, compared with MigrationContext.max_translation_attempts.
    translation_attempts: int = 0

    # map_visuals
    pages: list[PBIReportPage] = Field(default_factory=list)

    # assemble
    pbip_path: str | None = None

    # Refinement (only with MigrationContext.refine). The project's git baseline commit, and what's
    # still wrong, one item per page (replaced by every assess pass).
    refine_baseline: str | None = None
    work_items: list[WorkItem] = Field(default_factory=list)

    # Every node may append; the reducer concatenates instead of overwriting.
    issues: Annotated[list[MigrationIssue], operator.add] = Field(default_factory=list)


@dataclass(frozen=True)
class MigrationContext:
    """Per-run dependencies and settings, injected as ``runtime.context``."""

    # LangChain chat model; None runs the deterministic path (no translation, no LLM router).
    chat_model: Any | None = None
    # Run the external validators over the written project: TMDL (TOM) and PBIR
    # (powerbi-report-author). Each is skipped on its own when its tool isn't installed.
    run_output_validation: bool = True
    # Translate passes per calculated field: the first attempt plus repairs.
    max_translation_attempts: int = 2
    # After validation, let the refinement agent fix what the first pass got wrong (needs
    # chat_model and validation on; Power BI Desktop for the review). Off by default.
    refine: bool = False
    # Most pages the agent works on in one run.
    max_refine_items: int = 10
