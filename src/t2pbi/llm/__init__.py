"""LLM layer: builds the Azure OpenAI chat model. Prompts live with the code that uses them."""

from t2pbi.llm.client import build_chat_model, provider_error_message

__all__ = ["build_chat_model", "provider_error_message"]
