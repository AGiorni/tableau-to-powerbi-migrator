"""Shared test fixtures: the sample workbooks, a mapping shortcut, fake chat models, and the
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
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from t2pbi.ingest import load_workbook
from t2pbi.mapping.model_mapper import MappingResult, map_model
from t2pbi.mapping.report import MigrationReport
from t2pbi.parse import parse_workbook
from t2pbi.toolchain import find_dotnet_tool, find_node_tool

SAMPLES = Path(__file__).resolve().parents[1] / "samples"  # one folder per workbook

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
    return SAMPLES / "superstore" / "superstore.twb"


@pytest.fixture
def sales_sql_twb() -> Path:
    return SAMPLES / "sales_sql" / "sales_sql.twb"


@pytest.fixture
def book1_twb() -> Path:
    return SAMPLES / "Book1" / "Book1.twb"


@pytest.fixture
def book1_hidden_twb() -> Path:
    """Book1 with "Hide All Sheets" on its dashboard: its four worksheets are hidden."""

    return SAMPLES / "Book1_hidden" / "Book1_hidden.twb"


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


class ScriptedAgentChat(BaseChatModel):
    """A chat model that plays the refinement agent from a script, one entry per agent run.

    Each run's script is a list of steps: ``(tool_name, args)`` makes a tool call, a list of such
    pairs makes several calls in one message (as real models do), a string is the final answer.
    ``args`` may be a function of the tool results so far (``tool_results``, newest last), so a
    script can read a file and write back an edited copy.
    Calls outside the agent (DAX translation, visual routing) get an empty answer, as from a model
    that can't help, so the deterministic fallbacks apply.
    """

    scripts: list[list[Any]]
    briefs: list[str] = Field(default_factory=list)
    tool_results: list[str] = Field(default_factory=list)
    _run: int = 0
    _step: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        from t2pbi.refine.agent import SYSTEM_PROMPT

        is_agent = bool(messages) and isinstance(messages[0], SystemMessage) and messages[0].content == SYSTEM_PROMPT
        if not is_agent:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])
        if not any(isinstance(m, AIMessage) for m in messages):  # a new agent run
            self._run, self._step = self._run + 1, 0
            self.briefs.append(messages[1].content[0]["text"])
        else:
            last_ai = max(i for i, m in enumerate(messages) if isinstance(m, AIMessage))
            self.tool_results.extend(str(m.content) for m in messages[last_ai + 1:] if m.type == "tool")
        script = self.scripts[self._run - 1] if self._run <= len(self.scripts) else ["NO CHANGE: no script"]
        step = script[min(self._step, len(script) - 1)]
        self._step += 1
        if isinstance(step, str):
            message = AIMessage(content=step)
        else:
            calls = step if isinstance(step, list) else [step]
            message = AIMessage(content="", tool_calls=[
                {"name": name, "args": args(self.tool_results) if callable(args) else args,
                 "id": f"c{self._run}.{self._step}.{i}"}
                for i, (name, args) in enumerate(calls)
            ])
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.fixture
def scripted_agent_chat() -> type[ScriptedAgentChat]:
    """The :class:`ScriptedAgentChat` class."""

    return ScriptedAgentChat


@pytest.fixture
def first_pass_without(monkeypatch):
    """Make the first pass write a project with some of its own fixes undone, so the refinement
    loop has those problems to work on: ``first_pass_without(titles=["Key Performance
    Indicators"], tooltip_pages=["Tooltip: Profit Ratio by City"])`` titles those sheets' visuals
    with the sheet name and turns those tooltip pages back into ordinary pages, with no host link."""

    import json

    from t2pbi.graph import nodes

    def undo(titles: tuple[str, ...] | list[str] = (), tooltip_pages: tuple[str, ...] | list[str] = ()) -> None:
        original = nodes.assemble_pbip

        def assemble(*args, **kwargs):
            pbip = original(*args, **kwargs)
            pages = list(Path(pbip).parent.glob("*.Report/definition/pages/*/page.json"))
            names = {}
            for page_json in pages:
                page = json.loads(page_json.read_text(encoding="utf-8"))
                names[page["name"]] = page["displayName"]
                if page["displayName"] in tooltip_pages:
                    for key in ("type", "visibility", "pageBinding"):
                        page.pop(key, None)
                    page.update({"displayOption": "FitToPage", "width": 1920, "height": 1080})
                    page_json.write_text(json.dumps(page), encoding="utf-8")
            for visual_json in Path(pbip).parent.glob("*.Report/definition/pages/*/visuals/*/visual.json"):
                data = json.loads(visual_json.read_text(encoding="utf-8"))
                container = data["visual"].get("visualContainerObjects", {})
                page = names[visual_json.parent.parent.parent.name]
                if page in titles and "title" in container:
                    container["title"][0]["properties"]["text"]["expr"]["Literal"]["Value"] = f"'{page}'"
                link = container.get("visualTooltip")
                if link and names.get(link[0]["properties"]["section"]["expr"]["Literal"]["Value"].strip("'")) in tooltip_pages:
                    del container["visualTooltip"]
                visual_json.write_text(json.dumps(data), encoding="utf-8")
            return pbip

        monkeypatch.setattr(nodes, "assemble_pbip", assemble)

    return undo
