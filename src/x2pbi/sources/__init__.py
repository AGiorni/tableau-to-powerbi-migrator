"""Registry of source adapters.

To add a BI tool: write an adapter package under ``x2pbi/sources/<tool>/`` that satisfies
:class:`~x2pbi.sources.base.SourceAdapter`, then list it in :data:`ADAPTERS`. Nothing else in the
codebase needs to change.
"""

from __future__ import annotations

from pathlib import Path

from x2pbi.sources.base import (
    GENERIC_DIALECT,
    ExpressionDialect,
    SourceAdapter,
    UnknownSourceError,
)
from x2pbi.sources.spotfire import SPOTFIRE
from x2pbi.sources.tableau import TABLEAU

#: Every adapter the tool knows about, in the order they are offered to the user.
ADAPTERS: tuple[SourceAdapter, ...] = (TABLEAU, SPOTFIRE)

__all__ = [
    "ADAPTERS",
    "GENERIC_DIALECT",
    "ExpressionDialect",
    "SourceAdapter",
    "UnknownSourceError",
    "adapter_for_path",
    "get_adapter",
    "supported_extensions",
]


def get_adapter(name: str) -> SourceAdapter:
    """Return the adapter registered under ``name`` (e.g. ``"tableau"``)."""

    for adapter in ADAPTERS:
        if adapter.name == name.lower():
            return adapter
    known = ", ".join(a.name for a in ADAPTERS)
    raise UnknownSourceError(f"Unknown source '{name}'. Known sources: {known}.")


def adapter_for_path(path: str | Path) -> SourceAdapter:
    """Return the adapter that claims ``path``'s file extension."""

    suffix = Path(path).suffix.lower()
    for adapter in ADAPTERS:
        if suffix in adapter.extensions:
            return adapter
    raise UnknownSourceError(
        f"No source adapter handles '{suffix or Path(path).name}'. "
        f"Supported extensions: {', '.join(supported_extensions())}."
    )


def supported_extensions() -> list[str]:
    """Every file extension any registered adapter accepts."""

    return [ext for adapter in ADAPTERS for ext in adapter.extensions]
