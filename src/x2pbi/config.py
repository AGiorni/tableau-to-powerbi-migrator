"""Runtime configuration.

Values are read from the environment so the tool works both locally and in CI without code
changes. Azure OpenAI uses Entra ID (passwordless) auth by default via ``DefaultAzureCredential``;
set ``X2PBI_AZURE_OPENAI_API_KEY`` only if key-based auth is required.

The legacy ``T2PBI_*`` names are still honoured as a fallback, so an existing local environment
keeps working after the rename.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_LEGACY_PREFIX = "T2PBI_"
_PREFIX = "X2PBI_"


@dataclass(frozen=True)
class AzureOpenAIConfig:
    """Connection settings for the Azure OpenAI chat model."""

    endpoint: str | None
    deployment: str
    api_version: str
    api_key: str | None

    @property
    def is_configured(self) -> bool:
        return bool(self.endpoint and self.deployment)


def _env(suffix: str, default: str | None = None) -> str | None:
    """Read ``X2PBI_<suffix>``, falling back to the legacy ``T2PBI_<suffix>``."""

    return os.getenv(_PREFIX + suffix) or os.getenv(_LEGACY_PREFIX + suffix) or default


def load_azure_openai_config() -> AzureOpenAIConfig:
    return AzureOpenAIConfig(
        endpoint=_env("AZURE_OPENAI_ENDPOINT"),
        deployment=_env("AZURE_OPENAI_DEPLOYMENT", "gpt-4o") or "gpt-4o",
        api_version=_env("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")
        or "2024-08-01-preview",
        api_key=_env("AZURE_OPENAI_API_KEY"),
    )
