"""Runtime configuration.

Settings come from environment variables, so the tool works locally and in CI without code
changes. Locally they can also live in a ``.env`` file (gitignored; template in ``.env.example``),
which :func:`load_env_file` loads. **Real environment variables always win** over the file, and an
empty value counts as unset.

Azure OpenAI uses Entra ID (passwordless) auth by default via ``DefaultAzureCredential``; set
``T2PBI_AZURE_OPENAI_API_KEY`` only if key-based auth is required.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# src/t2pbi/config.py -> repository root, where a developer's .env lives.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def load_env_file(path: str | Path | None = None) -> Path | None:
    """Load ``KEY=value`` lines from a ``.env`` file into the environment; returns the file used.

    Without ``path``: ``$T2PBI_ENV_FILE`` if set (tests point it at nothing, to stay isolated from
    a developer's real settings), else the nearest ``.env`` from the current directory upwards,
    else the repository's. Variables already set in the environment are never overwritten. Safe
    to call more than once.
    """

    if path is None and os.getenv("T2PBI_ENV_FILE"):
        path = os.environ["T2PBI_ENV_FILE"]
    if path is not None:
        candidate: Path | None = Path(path)
    else:
        found = find_dotenv(usecwd=True)
        candidate = Path(found) if found else _REPO_ROOT / ".env"
    if candidate is None or not candidate.is_file():
        return None
    load_dotenv(candidate, override=False)
    return candidate


def _setting(name: str, default: str = "") -> str:
    """An environment setting; empty counts as unset, so a blank line in .env means "default"."""

    return os.getenv(name) or default


@dataclass(frozen=True)
class AzureOpenAIConfig:
    """Connection settings for the Azure OpenAI chat model."""

    endpoint: str | None
    deployment: str
    api_version: str
    api_key: str | None
    # None = don't send a temperature (the model's own default). Reasoning models (o-series,
    # gpt-5 family) reject anything but their default, so they need None.
    temperature: float | None = 0.0
    # Call the Responses API instead of Chat Completions. Reasoning models take tool calls (the
    # refinement agent) only there; needs API version 2025-03-01-preview or later.
    use_responses_api: bool = False

    @property
    def is_configured(self) -> bool:
        return bool(self.endpoint and self.deployment)


def load_azure_openai_config() -> AzureOpenAIConfig:
    load_env_file()
    return AzureOpenAIConfig(
        endpoint=_setting("T2PBI_AZURE_OPENAI_ENDPOINT") or None,
        deployment=_setting("T2PBI_AZURE_OPENAI_DEPLOYMENT", "gpt-4o"),
        api_version=_setting("T2PBI_AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
        api_key=_setting("T2PBI_AZURE_OPENAI_API_KEY") or None,
        temperature=_temperature(_setting("T2PBI_AZURE_OPENAI_TEMPERATURE", "0")),
        use_responses_api=_flag("T2PBI_AZURE_OPENAI_RESPONSES_API"),
    )


@dataclass(frozen=True)
class PhoenixConfig:
    """Tracing to a local Arize Phoenix (t2pbi.observe.phoenix)."""

    enabled: bool = False
    endpoint: str = "http://localhost:6006"
    project: str = "t2pbi"


def load_phoenix_config() -> PhoenixConfig:
    load_env_file()
    return PhoenixConfig(
        enabled=_flag("T2PBI_PHOENIX"),
        endpoint=_setting("T2PBI_PHOENIX_ENDPOINT", "http://localhost:6006").rstrip("/"),
        project=_setting("T2PBI_PHOENIX_PROJECT", "t2pbi"),
    )


def _flag(name: str) -> bool:
    raw = _setting(name, "false").strip().lower()
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def _temperature(raw: str) -> float | None:
    """``default`` (or ``none``) means "send no temperature"; otherwise a number. Default 0, the
    most repeatable setting, for models that accept it."""

    if raw.strip().lower() in ("default", "none"):
        return None
    try:
        return float(raw)
    except ValueError:
        raise ValueError(
            f"T2PBI_AZURE_OPENAI_TEMPERATURE must be a number or 'default', got {raw!r}"
        ) from None
