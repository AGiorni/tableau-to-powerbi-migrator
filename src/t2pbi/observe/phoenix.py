"""Optional traces of every run in a local Arize Phoenix: each graph step, model call (prompt,
images, answer, tokens) and tool call, browsable at http://localhost:6006.

Off unless ``T2PBI_PHOENIX=true``. It uses OpenInference's LangChain instrumentation, which hooks
LangChain's callback system for the whole process, so nothing in the graph changes. Traces go
only to the configured endpoint, by default a Phoenix server on this machine. Start it with
``python scripts/phoenix_server.py``, which turns Phoenix's own usage telemetry off: traces
carry the prompts, the report JSON and the screenshots, and must not leave the machine.

Needs the ``trace`` extra (``pip install -e ".[trace]"``). Without it, or without a server
listening, tracing is skipped with a message and the run goes on.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request

from t2pbi.config import load_phoenix_config

_enabled_at: str | None = None


def enable_phoenix() -> str | None:
    """Start sending traces to Phoenix if it's configured and reachable; returns the URL of its
    UI, or None. Safe to call more than once."""

    global _enabled_at
    if _enabled_at is not None:
        return _enabled_at
    config = load_phoenix_config()
    if not config.enabled:
        return None
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from phoenix.otel import register
    except ImportError:
        _say('T2PBI_PHOENIX is on but the tracing packages are missing: pip install -e ".[trace]"')
        return None
    if not _reachable(config.endpoint):
        _say(f"T2PBI_PHOENIX is on but no Phoenix is listening at {config.endpoint}; "
             "start it with `python scripts/phoenix_server.py`. Running without traces.")
        return None
    provider = register(
        endpoint=f"{config.endpoint}/v1/traces",
        project_name=config.project,
        batch=True,
        set_global_tracer_provider=False,
        verbose=False,
    )
    LangChainInstrumentor().instrument(tracer_provider=provider)
    _enabled_at = f"{config.endpoint}/projects"
    _say(f"Tracing to Phoenix: {config.endpoint} (project {config.project!r})")
    return _enabled_at


def _reachable(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(endpoint, timeout=2):
            return True
    except urllib.error.HTTPError:
        return True  # it answered
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
