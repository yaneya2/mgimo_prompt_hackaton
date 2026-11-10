"""Model access for agents; no HTTP endpoint or automatic startup calls."""
from app.llm.base import ModelAdapter, ModelError, ModelResult
from app.llm.openrouter import OpenRouterAdapter
from app.llm.codex import CodexAdapter
from app.llm.factory import create_model
from app.llm.usage import JsonlUsageSink
from app.llm.accounting import UsageRecorder, TokenPricing

__all__ = ["ModelAdapter", "ModelError", "ModelResult", "CodexAdapter", "OpenRouterAdapter", "JsonlUsageSink", "UsageRecorder", "TokenPricing", "create_model"]
