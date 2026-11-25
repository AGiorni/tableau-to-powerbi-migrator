"""Shared test fixtures: the sample workbooks, a mapping shortcut, a fake chat model, and the
``external`` marker for tests that need Microsoft's tools.

``@pytest.mark.external("powerbi-report-author", "tmdl-validate")`` skips a test when a named tool
isn't installed (see CLAUDE.md, "External tools"). In CI, ``T2PBI_REQUIRE_TOOLS=1`` turns that
skip into a failure, so a broken CI setup can't hide behind skipped tests.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from t2pbi.ingest import load_workbook
from t2pbi.mapping.model_mapper import MappingResult, map_model
from t2pbi.mapping.report import MigrationReport
from t2pbi.parse import parse_workbook
from t2pbi.toolchain import find_dotnet_tool, find_node_tool

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "input"

# How to find each external tool a test may ask for.
_TOOL_FINDERS = {
    "powerbi-report-author": find_node_tool,
    "tmdl-validate": find_dotnet_tool,
}


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "external(*tools): needs these external tools; skipped without them"
    )


def pytest_runtest_setup(item):
    marker = item.get_closest_marker("external")
    if marker is None:
        return
    missing = [tool for tool in marker.args if _TOOL_FINDERS[tool](tool) is None]
    if not missing:
        return
    message = f"needs {', '.join(missing)} (see CLAUDE.md, External tools)"
    if os.getenv("T2PBI_REQUIRE_TOOLS"):
        pytest.fail(f"{message}; T2PBI_REQUIRE_TOOLS is set, so this can't be skipped", pytrace=False)
    pytest.skip(message)


@pytest.fixture
def superstore_twb() -> Path:
    return SAMPLES / "superstore.twb"


@pytest.fixture
def sales_sql_twb() -> Path:
    return SAMPLES / "sales_sql.twb"


@pytest.fixture
def book1_twb() -> Path:
    return SAMPLES / "Book1.twb"


@pytest.fixture
def mapped() -> Callable[[Path], MappingResult]:
    """Factory: workbook path -> its mapped Power BI model (ingest, parse, map_model)."""

    def _mapped(path: Path) -> MappingResult:
        return map_model(parse_workbook(load_workbook(path)), MigrationReport())

    return _mapped


class FakeChat:
    """Stand-in for a LangChain chat model. Records each prompt and answers from ``reply``.

    ``reply`` is one of: a string (always returned), a list of strings (returned in order, the last
    one repeating), a callable ``prompt -> str``, or an exception (raised on every call).
    """

    def __init__(self, reply: str | list[str] | Callable[[str], str] | Exception) -> None:
        self._reply = reply
        self.prompts: list[str] = []

    def invoke(self, messages):
        prompt = messages[-1].content
        self.prompts.append(prompt)
        reply = self._reply
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return SimpleNamespace(content=reply(prompt))
        if isinstance(reply, list):
            return SimpleNamespace(content=reply[min(len(self.prompts), len(reply)) - 1])
        return SimpleNamespace(content=reply)


@pytest.fixture
def fake_chat() -> type[FakeChat]:
    """The :class:`FakeChat` class, so tests can build one without importing conftest."""

    return FakeChat
