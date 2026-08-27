"""Runtime configuration.

Values are read from the environment so the tool works both locally and in CI without code
changes. Azure OpenAI uses Entra ID (passwordless) auth by default via ``DefaultAzureCredential``;
set ``T2PBI_AZURE_OPENAI_API_KEY`` only if key-based auth is required.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


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


def load_azure_openai_config() -> AzureOpenAIConfig:
    return AzureOpenAIConfig(
        endpoint=os.getenv("T2PBI_AZURE_OPENAI_ENDPOINT"),
        deployment=os.getenv("T2PBI_AZURE_OPENAI_DEPLOYMENT", "gpt-4o"),
        api_version=os.getenv("T2PBI_AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
        api_key=os.getenv("T2PBI_AZURE_OPENAI_API_KEY"),
    )
