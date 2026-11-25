"""Shared test fixtures: the sample workbooks, a mapping shortcut, and a fake chat model."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from t2pbi.ingest import load_workbook
from t2pbi.mapping.model_mapper import MappingResult, map_model
from t2pbi.mapping.report import MigrationReport
from t2pbi.parse import parse_workbook

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "input"


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
