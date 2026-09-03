"""Spotfire source adapter: ``.dxp`` → source IR.

Registered and wired, but not yet implemented — :func:`parse` raises ``NotImplementedError``
with a pointer to what is left to do. Everything downstream of the source IR (model mapping,
Power Query generation, visual mapping, DAX translation, TMDL/PBIR emission) already works;
finishing Spotfire support means filling in ``parser.py``.
"""

from __future__ import annotations

from pathlib import Path

from x2pbi.ir.source_ir import SourceWorkbook
from x2pbi.sources.base import ExpressionDialect
from x2pbi.sources.spotfire.loader import load_analysis
from x2pbi.sources.spotfire.parser import parse_analysis

SPOTFIRE_DIALECT = ExpressionDialect(
    name="Spotfire",
    language="the Spotfire expression language",
    scoped_aggregation_label="OVER() node-navigation expression",
    rules=(
        (
            "Spotfire custom expressions aggregate explicitly (Sum([Sales])); map each "
            "aggregation to its DAX equivalent rather than relying on implicit measure behaviour."
        ),
        (
            "Translate OVER(...) / node-navigation expressions (Parent, Previous, AllNext, ...) "
            "using CALCULATE with ALL/ALLEXCEPT/VALUES or a time-intelligence function."
        ),
        (
            "Spotfire's THEN clause applies a second pass over the first result; express it as a "
            "nested CALCULATE or a variable in a DAX VAR/RETURN block."
        ),
    ),
)


class SpotfireSource:
    """Adapter for Spotfire analysis files."""

    name = "spotfire"
    display_name = "Spotfire"
    extensions = (".dxp",)
    dialect = SPOTFIRE_DIALECT

    def parse(self, path: str | Path) -> SourceWorkbook:
        return parse_analysis(load_analysis(path))


SPOTFIRE = SpotfireSource()

__all__ = ["SPOTFIRE", "SPOTFIRE_DIALECT", "SpotfireSource", "load_analysis", "parse_analysis"]
