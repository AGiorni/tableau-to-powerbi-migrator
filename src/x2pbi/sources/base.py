"""The contract a source adapter satisfies.

An adapter is the *only* thing that knows a particular BI tool's file format and expression
language. It has two jobs:

1. ``parse`` a native file into the neutral :class:`~x2pbi.ir.source_ir.SourceWorkbook`.
2. Describe its ``dialect`` so the expression translator can prompt for the right language.

Everything else — model mapping, Power Query generation, visual mapping, validation, TMDL/PBIR
emission — is shared and never learns which tool the input came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from x2pbi.ir.source_ir import SourceWorkbook


@dataclass(frozen=True)
class ExpressionDialect:
    """What the expression translator needs to know about a source's formula language."""

    #: Tool name as it appears in the prompt, e.g. ``"Tableau"``.
    name: str
    #: How to refer to the formula language, e.g. ``"the Tableau calculation language"``.
    language: str = "the source tool's calculation language"
    #: What the source calls an aggregation scoped outside the view, e.g. ``"LOD expression"``.
    scoped_aggregation_label: str = "scoped aggregation"
    #: Extra translation rules appended to the system prompt, one bullet each.
    rules: tuple[str, ...] = ()


# A dialect for callers that have no adapter in hand (direct API use, tests).
GENERIC_DIALECT = ExpressionDialect(name="the source tool")


@runtime_checkable
class SourceAdapter(Protocol):
    """Turns one BI tool's files into the neutral source IR."""

    #: Short registry key, e.g. ``"tableau"``. Used by ``--source`` on the CLI.
    name: str
    #: Human-readable tool name for messages, e.g. ``"Tableau"``.
    display_name: str
    #: Lower-case file extensions this adapter claims, e.g. ``(".twb", ".twbx")``.
    extensions: tuple[str, ...]
    #: How this tool's formula language should be described to the translator.
    dialect: ExpressionDialect

    def parse(self, path: str | Path) -> SourceWorkbook:
        """Load and parse ``path`` into a :class:`SourceWorkbook`."""
        ...


class UnknownSourceError(ValueError):
    """Raised when no registered adapter handles a requested source or file extension."""
