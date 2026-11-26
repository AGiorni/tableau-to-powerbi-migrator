"""Client for Microsoft's Power BI Authoring MCP server (``powerbi-modeling-mcp``).

Talks to a semantic model open in Power BI Desktop (or a PBIP folder): connect, refresh data, run
DAX. The server is pinned in ``tools/package.json``; one server process is one session, and the
model connection lives in that session, so every call goes through one :class:`ModelingServer`.

**EULA.** The server refuses every call until its EULA is accepted. t2pbi never accepts it: the
operator does, by setting ``PBI_MODELING_MCP_ACCEPT_EULA=true`` (e.g. in ``.env``), which the
server reads from its environment.
"""

from __future__ import annotations

import csv
import io
import json
import os
from typing import Any, Self

from t2pbi.toolchain import find_node_tool

_SERVER = "powerbi-modeling-mcp-win32-x64"  # the platform binary the npm package installs
_EULA_VARIABLE = "PBI_MODELING_MCP_ACCEPT_EULA"


class ModelingError(RuntimeError):
    """The MCP server is unavailable, refused, or reported an error."""


def eula_accepted() -> bool:
    return os.getenv(_EULA_VARIABLE, "").strip().lower() in ("1", "true", "yes")


class ModelingServer:
    """Async context manager around one Authoring MCP server session."""

    def __init__(self, read_write: bool = False) -> None:
        self._mode = "--read-write" if read_write else "--read-only"
        self._session = None
        self._stack = None

    async def __aenter__(self) -> Self:
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        if not eula_accepted():
            raise ModelingError(
                f"The Power BI Authoring MCP server needs its EULA accepted by you: review "
                f"https://go.microsoft.com/fwlink/?LinkId=2381247, then set {_EULA_VARIABLE}=true."
            )
        exe = find_node_tool(_SERVER)
        if exe is None:
            raise ModelingError("powerbi-modeling-mcp not installed (npm ci in tools/)")
        params = StdioServerParameters(command="cmd", args=["/c", exe, self._mode], env=dict(os.environ))
        self._stack = AsyncExitStack()
        # The server logs every startup step to stderr; it also keeps its own audit log on disk.
        # devnull opens instantly, and the exit stack closes it.
        quiet = self._stack.enter_context(open(os.devnull, "w"))  # noqa: ASYNC230, SIM115
        read, write = await self._stack.enter_async_context(stdio_client(params, errlog=quiet))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._stack is not None:
            await self._stack.aclose()

    async def call(self, tool: str, request: dict[str, Any]) -> Any:
        """Call one tool; returns its JSON payload, or the text of a resource it returned."""

        assert self._session is not None, "use ModelingServer as an async context manager"
        result = await self._session.call_tool(tool, {"request": request})
        texts = []
        for block in result.content:
            resource = getattr(block, "resource", None)
            texts.append(getattr(resource, "text", None) if resource else getattr(block, "text", None))
        text = "\n".join(t for t in texts if t)
        # mcp 2.x renamed isError to is_error; both majors are supported.
        if getattr(result, "is_error", None) or getattr(result, "isError", False):
            raise ModelingError(f"{tool} {request.get('operation')}: {text[:500]}")
        try:
            payload = json.loads(text) if text else {}
        except ValueError:
            return text  # e.g. the CSV of a DAX query result
        if isinstance(payload, dict) and "EULA must be accepted" in str(payload.get("message", "")):
            raise ModelingError(payload["message"])
        return payload

    async def connect_desktop(self, window_title: str) -> str:
        """Connect to the engine of the Desktop window titled ``window_title``; returns its name."""

        listing = await self.call("connection_operations", {"operation": "ListLocalInstances"})
        matches = [i for i in listing.get("data", []) if i.get("parentWindowTitle") == window_title]
        if len(matches) != 1:
            raise ModelingError(
                f"expected one Power BI Desktop window titled {window_title!r}, found {len(matches)}"
            )
        connected = await self.call(
            "connection_operations",
            {"operation": "Connect", "ConnectionString": f"Provider=MSOLAP;Data Source=localhost:{matches[0]['port']}"},
        )
        return str(connected.get("data", ""))

    async def refresh(self) -> None:
        """Load the model's data from its sources (a freshly migrated project opens empty)."""

        await self.call("model_operations", {"operation": "RefreshWithXMLA", "RefreshType": "Full"})

    async def query(self, dax: str) -> list[dict[str, str]]:
        """Run a DAX query; returns its rows, column name -> text value."""

        result = await self.call("dax_query_operations", {"operation": "Execute", "Query": dax})
        if not isinstance(result, str):
            raise ModelingError(f"DAX query returned no rows: {str(result)[:300]}")
        return parse_csv_rows(result)

    async def validate_dax(self, dax: str) -> str | None:
        """Check a DAX query with the engine; returns its error message, or None if valid."""

        result = await self.call("dax_query_operations", {"operation": "Validate", "Query": dax})
        data = result.get("data", {}) if isinstance(result, dict) else {}
        return None if data.get("isValid") else str(data.get("errorMessage") or result)


def parse_csv_rows(text: str) -> list[dict[str, str]]:
    """Rows of the CSV the server writes for a DAX result; ``[Name]`` headers become ``Name``."""

    reader = csv.reader(io.StringIO(text.strip()))
    rows = list(reader)
    if not rows:
        return []
    header = [h.strip()[1:-1] if h.strip().startswith("[") and h.strip().endswith("]") else h.strip() for h in rows[0]]
    return [dict(zip(header, row, strict=False)) for row in rows[1:]]
