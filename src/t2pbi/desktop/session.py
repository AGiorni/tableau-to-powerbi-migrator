"""A migrated project open in Power BI Desktop, with its data loaded: query it, screenshot it.

    async with desktop_session(pbip_path) as desktop:
        rows = await desktop.query("EVALUATE ROW(\"Sales\", SUM('Orders'[Sales]))")
        shots = desktop.screenshot_pages(out_dir)

Opening launches Desktop through the Desktop Bridge, waits until the project is loaded, connects
the Authoring MCP server to Desktop's engine and refreshes the data (a freshly written project
opens empty). Desktop is always closed on exit, even on errors.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from t2pbi.desktop import bridge
from t2pbi.desktop.modeling import ModelingServer


@dataclass
class DesktopSession:
    pid: int
    pbip: Path
    server: ModelingServer

    async def query(self, dax: str) -> list[dict[str, str]]:
        return await self.server.query(dax)

    def screenshot_pages(self, out_dir: Path) -> dict[str, Path]:
        """Every report page as a PNG; page display name -> file."""

        return bridge.screenshot_all(self.pid, out_dir)


@asynccontextmanager
async def desktop_session(pbip: Path, refresh: bool = True) -> AsyncIterator[DesktopSession]:
    pbip = Path(pbip).resolve()
    pid = bridge.open_report(pbip)
    try:
        bridge.wait_ready(pid, pbip)
        async with ModelingServer(read_write=True) as server:
            await server.connect_desktop(window_title=pbip.stem)
            if refresh:
                await server.refresh()
            yield DesktopSession(pid=pid, pbip=pbip, server=server)
    finally:
        bridge.close(pid)
