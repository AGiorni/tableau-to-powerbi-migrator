"""Construct an Azure OpenAI chat model.

Prefers Entra ID (passwordless) auth via ``DefaultAzureCredential``; falls back to an API key
only when ``T2PBI_AZURE_OPENAI_API_KEY`` is set. Returns ``None`` when Azure OpenAI is not
configured so callers can degrade to the deterministic path.
"""

from __future__ import annotations

from typing import Any

from t2pbi.config import AzureOpenAIConfig, load_azure_openai_config


def build_chat_model(config: AzureOpenAIConfig | None = None) -> Any | None:
    """Return a configured ``AzureChatOpenAI`` instance, or ``None`` if unavailable."""

    config = config or load_azure_openai_config()
    if not config.is_configured:
        return None

    try:
        from langchain_openai import AzureChatOpenAI
    except ImportError:
        return None

    common = {
        "azure_endpoint": config.endpoint,
        "azure_deployment": config.deployment,
        "api_version": config.api_version,
        "temperature": 0,
    }

    if config.api_key:
        return AzureChatOpenAI(api_key=config.api_key, **common)

    token_provider = _entra_token_provider(config.endpoint or "")
    if token_provider is None:
        return None
    return AzureChatOpenAI(azure_ad_token_provider=token_provider, **common)


def _entra_token_provider(endpoint: str):
    """Build a bearer-token provider for Azure OpenAI using DefaultAzureCredential."""

    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    except ImportError:
        return None

    credential = DefaultAzureCredential()
    return get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default"
    )
