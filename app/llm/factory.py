"""The single provider-selection point used by agents."""
from app.config import Settings
from app.llm.base import ModelAdapter, ModelError
from app.llm.codex import CodexAdapter
from app.llm.openrouter import OpenRouterAdapter
from app.llm.usage import UsageSink


def create_model(settings: Settings, sink: UsageSink) -> ModelAdapter:
    if settings.model_provider == "codex":
        return CodexAdapter(settings, sink)
    if settings.model_provider == "openrouter":
        return OpenRouterAdapter(settings, sink)
    raise ModelError("CONFIGURATION_ERROR", "APP_MODEL_PROVIDER must be codex or openrouter")
