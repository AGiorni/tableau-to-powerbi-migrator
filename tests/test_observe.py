"""Watching a run: the live log, and the opt-in Phoenix tracing (no server needed here)."""

from __future__ import annotations

import io
import json

from t2pbi.graph import MigrationContext, migrate, nodes
from t2pbi.observe import LiveLog
from t2pbi.observe import phoenix as phoenix_module

KPI = "Key Performance Indicators"


def _retitle(results: list[str]) -> dict:
    data = json.loads(results[-1])
    title = data["visual"]["visualContainerObjects"]["title"][0]["properties"]["text"]["expr"]["Literal"]
    title["Value"] = "'Executive Overview - Profitability'"
    return {"visual": KPI, "content": json.dumps(data)}


def test_the_live_log_follows_steps_agent_calls_and_decisions(book1_twb, tmp_path, monkeypatch, scripted_agent_chat):
    monkeypatch.setattr(nodes, "run_pbir_validation", lambda _path: None)
    monkeypatch.setattr(nodes.tmdl_cli, "run_tmdl_validation", lambda _path: None)
    monkeypatch.setattr(nodes, "validator_errors", lambda _path: [])
    monkeypatch.setattr(nodes, "report_errors", lambda _path: [])
    chat = scripted_agent_chat(scripts=[[
        ("read_visual", {"visual": KPI}), ("write_visual", _retitle), "CHANGED: set the title",
    ]])
    out = io.StringIO()
    context = MigrationContext(
        chat_model=chat, refine=True, use_desktop=False, max_refine_items=1, max_translation_attempts=1,
        callbacks=(LiveLog(stream=out, max_agent_steps=25),),
    )

    migrate(book1_twb, tmp_path, context)

    log = out.getvalue().splitlines()
    starts = [line[2:] for line in log if line.startswith("> ")]
    assert starts[0] == "ingest" and starts[-1] == "finalize_refine"
    assert "route_refine" not in starts  # routers aren't steps
    assert f"fix_item: {KPI} (6 problem(s))" in starts
    assert "    model call 1/25" in log
    assert f"      -> read_visual(visual='{KPI}')" in log
    assert any(line.startswith("      <- read_visual: ") and line.endswith(" characters") for line in log)
    # A whole visual.json is shown by its size, not printed.
    assert any(line.startswith(f"      -> write_visual(visual='{KPI}', content=<") for line in log)
    assert any("Validation: no errors." in line for line in log)
    assert any(line.startswith(f"  * {KPI}: Kept as commit ") for line in log)
    assert any(line.startswith("  done fix_item (") and "model call(s)" in line for line in log)


def test_the_live_log_never_breaks_a_run_on_an_unprintable_character():
    class Cp1252(io.StringIO):
        encoding = "cp1252"

    out = Cp1252()
    LiveLog(stream=out)._print("title “Executive Overview – Profitability” → ✓")
    assert out.getvalue().startswith("title ")  # printed, with what cp1252 can't show replaced


def test_phoenix_tracing_is_off_unless_asked_for(monkeypatch, tmp_path):
    monkeypatch.setenv("T2PBI_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("T2PBI_PHOENIX", raising=False)
    monkeypatch.setattr(phoenix_module, "_enabled_at", None)
    assert phoenix_module.enable_phoenix() is None


def test_phoenix_tracing_is_skipped_when_no_server_listens(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("T2PBI_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("T2PBI_PHOENIX", "true")
    monkeypatch.setenv("T2PBI_PHOENIX_ENDPOINT", "http://127.0.0.1:9")  # nothing listens there
    monkeypatch.setattr(phoenix_module, "_enabled_at", None)
    monkeypatch.setattr(phoenix_module, "_reachable", lambda _endpoint: False)

    assert phoenix_module.enable_phoenix() is None
    message = capsys.readouterr().err
    assert "no Phoenix is listening" in message or "tracing packages are missing" in message
