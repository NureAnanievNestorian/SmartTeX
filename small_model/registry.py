from __future__ import annotations

from .deepseek_provider import DeepSeekProvider
from .gemini_provider import GeminiProvider
from .mock_provider import MockProvider
from .provider import SmallModelProvider


def get_provider(name: str | None = None, model_name: str | None = None, config: dict | None = None) -> SmallModelProvider:
    provider_name = (name or "mock").strip().lower()
    config = config or {}
    if provider_name == "mock":
        return MockProvider()
    if provider_name == "gemini":
        return GeminiProvider(model_name=model_name, config=config)
    if provider_name == "deepseek":
        return DeepSeekProvider(model_name=model_name, config=config)
    raise ValueError(f"Unknown small model provider: {provider_name}")
