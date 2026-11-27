"""A live log of a migration in the terminal: each graph step, and inside the refinement agent
each model call and tool call, as they happen.

It's a LangChain callback handler: ``migrate()`` passes it to the graph in ``config["callbacks"]``
(from ``MigrationContext.callbacks``), and LangGraph hands it every event from every node,
including the model and tool calls inside them. Events are told apart by their metadata: a graph
step is a chain whose name is its own ``langgraph_node`` with no checkpoint namespace; a call made
inside a step carries that step's name as the first part of its ``checkpoint_ns``.

It only prints; it never changes what the graph does, and a printing error never stops a run.
Output goes to stderr, so the CLI's own results on stdout stay clean; plain ASCII, because the
Windows console's default code page can't print arrows or ticks.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, TextIO
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

# Report issues from these stages are printed as they're produced (the refinement's decisions).
_LIVE_STAGES = {"refine"}
_WIDTH = 160


class LiveLog(BaseCallbackHandler):
    """Prints a migration's progress. ``max_agent_steps`` shows the agent's budget ("call 3/25")."""

    raise_error = False  # a printing problem must never stop a migration

    def __init__(self, stream: TextIO | None = None, max_agent_steps: int | None = None) -> None:
        self.stream = stream or sys.stderr
        self.max_agent_steps = max_agent_steps
        self._lock = threading.Lock()
        self._steps: dict[UUID, tuple[str, float]] = {}  # run id of a graph step -> (name, start)
        self._calls: dict[str, int] = {}  # graph step -> model calls made in it
        self._tokens: dict[str, int] = {}
        self._tools: dict[UUID, str] = {}

    # ---------------------------------------------------------------- graph steps

    def on_chain_start(self, serialized, inputs, *, run_id, metadata=None, name=None, **kwargs) -> None:
        metadata = metadata or {}
        if metadata.get("checkpoint_ns") or name != metadata.get("langgraph_node") or not name:
            return  # a router, or a chain inside a step
        self._steps[run_id] = (name, time.monotonic())
        self._calls[name] = self._tokens[name] = 0
        self._print(f"> {name}{_step_label(name, inputs)}")

    def on_chain_end(self, outputs, *, run_id, **kwargs) -> None:
        step = self._steps.pop(run_id, None)
        if step is None:
            return
        name, started = step
        for issue in (outputs.get("issues") or []) if isinstance(outputs, dict) else []:
            if getattr(issue, "stage", None) in _LIVE_STAGES or getattr(issue, "severity", None) == "error":
                self._print(f"  * {issue.object_name}: {issue.message}")
        calls = self._calls.get(name, 0)
        extra = f", {calls} model call(s), {self._tokens.get(name, 0):,} tokens" if calls else ""
        self._print(f"  done {name} ({time.monotonic() - started:.1f}s{extra})")

    def on_chain_error(self, error, *, run_id, **kwargs) -> None:
        step = self._steps.pop(run_id, None)
        if step is not None:
            self._print(f"  FAILED {step[0]}: {type(error).__name__}: {error}")

    # ---------------------------------------------------------------- model and tool calls

    def on_chat_model_start(self, serialized, messages, *, metadata=None, **kwargs) -> None:
        step = _step_of(metadata)
        self._calls[step] = self._calls.get(step, 0) + 1
        if step == "fix_item":
            budget = f"/{self.max_agent_steps}" if self.max_agent_steps else ""
            self._print(f"    model call {self._calls[step]}{budget}")

    def on_llm_end(self, response, **kwargs) -> None:
        usage = 0
        for generations in response.generations:
            for generation in generations:
                meta = getattr(getattr(generation, "message", None), "usage_metadata", None) or {}
                usage += int(meta.get("total_tokens") or 0)
        # This event carries no metadata; graph steps run one at a time, so it's the running one's.
        running = [name for name, _ in self._steps.values()]
        step = running[-1] if running else "?"
        self._tokens[step] = self._tokens.get(step, 0) + usage

    def on_tool_start(self, serialized, input_str, *, run_id, inputs=None, **kwargs) -> None:
        name = (serialized or {}).get("name") or "tool"
        self._tools[run_id] = name
        self._print(f"      -> {name}({_args(inputs, input_str)})")

    def on_tool_end(self, output, *, run_id, **kwargs) -> None:
        name = self._tools.pop(run_id, "tool")
        text = str(getattr(output, "content", output))
        if name in ("read_page", "read_visual"):
            self._print(f"      <- {name}: {len(text):,} characters")
            return
        lines = [line for line in text.splitlines() if line.strip()]
        first = lines[0] if lines else "(empty)"
        more = f" (+{len(lines) - 1} line(s))" if len(lines) > 1 else ""
        self._print(f"      <- {name}: {first}{more}")

    def on_tool_error(self, error, *, run_id, **kwargs) -> None:
        self._print(f"      FAILED {self._tools.pop(run_id, 'tool')}: {type(error).__name__}: {error}")

    # ---------------------------------------------------------------- output

    def _print(self, line: str) -> None:
        if len(line) > _WIDTH:
            line = line[: _WIDTH - 3] + "..."
        encoding = getattr(self.stream, "encoding", None) or "utf-8"
        line = line.encode(encoding, errors="replace").decode(encoding)  # e.g. "…" on cp1252
        with self._lock:
            print(line, file=self.stream, flush=True)


def _step_of(metadata: dict | None) -> str:
    metadata = metadata or {}
    namespace = metadata.get("checkpoint_ns") or ""
    return namespace.split(":", 1)[0] if namespace else str(metadata.get("langgraph_node") or "?")


def _step_label(name: str, inputs: Any) -> str:
    """For the agent and the review, the page they work on."""

    if name not in ("fix_item", "review_item", "pick_item"):
        return ""
    item = inputs.get("current_item") if isinstance(inputs, dict) else getattr(inputs, "current_item", None)
    if name == "pick_item":
        items = inputs.get("work_items") if isinstance(inputs, dict) else getattr(inputs, "work_items", None)
        item = items[0] if items else None
    page = item.get("page") if isinstance(item, dict) else getattr(item, "page", None)
    problems = item.get("problems") if isinstance(item, dict) else getattr(item, "problems", None)
    if not page:
        return ""
    return f": {page}" + (f" ({len(problems)} problem(s))" if name == "fix_item" and problems else "")


def _args(inputs: dict | None, input_str: str) -> str:
    """A tool call's arguments, short: long values (a whole visual.json) shown by size."""

    if not isinstance(inputs, dict):
        return input_str[:80]
    parts = []
    for key, value in inputs.items():
        text = value if isinstance(value, str) else repr(value)
        parts.append(f"{key}=<{len(text):,} chars>" if len(text) > 60 else f"{key}={text!r}")
    return ", ".join(parts)
