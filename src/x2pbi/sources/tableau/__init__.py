"""Tableau source adapter: ``.twb`` / ``.twbx`` → source IR."""

from __future__ import annotations

from pathlib import Path

from x2pbi.ir.source_ir import SourceWorkbook
from x2pbi.sources.base import ExpressionDialect
from x2pbi.sources.tableau.loader import LoadedWorkbook, load_workbook
from x2pbi.sources.tableau.parser import parse_workbook

TABLEAU_DIALECT = ExpressionDialect(
    name="Tableau",
    language="the Tableau calculation language",
    scoped_aggregation_label="LOD expression ({FIXED}/{INCLUDE}/{EXCLUDE})",
    rules=(
        (
            "Preserve aggregation semantics. Tableau row-level vs aggregate context maps to DAX "
            "iterator vs aggregation functions."
        ),
        (
            "Translate LOD expressions ({FIXED}/{INCLUDE}/{EXCLUDE}) using CALCULATE + "
            "ALLEXCEPT/ALL/VALUES as appropriate."
        ),
    ),
)


class TableauSource:
    """Adapter for Tableau workbooks."""

    name = "tableau"
    display_name = "Tableau"
    extensions = (".twb", ".twbx")
    dialect = TABLEAU_DIALECT

    def parse(self, path: str | Path) -> SourceWorkbook:
        return parse_workbook(load_workbook(path))


TABLEAU = TableauSource()

__all__ = ["TABLEAU", "TABLEAU_DIALECT", "LoadedWorkbook", "TableauSource", "load_workbook",
           "parse_workbook"]
