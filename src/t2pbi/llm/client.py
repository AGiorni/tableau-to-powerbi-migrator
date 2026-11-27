"""Construct an Azure OpenAI chat model.

Prefers Entra ID (passwordless) auth via ``DefaultAzureCredential``; falls back to an API key
only when ``T2PBI_AZURE_OPENAI_API_KEY`` is set. Returns ``None`` when Azure OpenAI is not
configured so callers can degrade to the deterministic path.
"""

from __future__ import annotations

from typing import Any

from t2pbi.config import AzureOpenAIConfig, load_azure_openai_config


def provider_error_message(exc: Exception, limit: int = 300) -> str:
    """A one-line, bounded description of a failed model call, for the migration report."""

    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def message_text(message: Any) -> str:
    """The text of a model's answer. Chat Completions returns a string; the Responses API returns
    a list of content blocks (text, reasoning, tool calls), whose text blocks are joined."""

    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block if isinstance(block, str) else str(block.get("text", ""))
            for block in content
            if isinstance(block, str) or (isinstance(block, dict) and block.get("type") == "text")
        )
    return ""


def build_chat_model(config: AzureOpenAIConfig | None = None) -> Any | None:
    """Return a configured ``AzureChatOpenAI`` instance, or ``None`` if unavailable."""

    config = config or load_azure_openai_config()
    if not config.is_configured:
        return None

    # Imported here, not at module level, so the deterministic path never pays to load them.
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from langchain_openai import AzureChatOpenAI

    common: dict[str, Any] = {
        "azure_endpoint": config.endpoint,
        "azure_deployment": config.deployment,
        "api_version": config.api_version,
    }
    if config.temperature is not None:  # reasoning models accept only their default
        common["temperature"] = config.temperature
    if config.use_responses_api:
        common["use_responses_api"] = True

    if config.api_key:
        return AzureChatOpenAI(api_key=config.api_key, **common)

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    return AzureChatOpenAI(azure_ad_token_provider=token_provider, **common)
