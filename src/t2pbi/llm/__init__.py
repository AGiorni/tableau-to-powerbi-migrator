"""LLM layer: builds the Azure OpenAI chat model. Prompts live with the mapping that uses them."""

from t2pbi.llm.client import build_chat_model

__all__ = ["build_chat_model"]
