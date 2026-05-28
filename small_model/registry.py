from __future__ import annotations

from django.conf import settings

from .gemini_provider import GeminiProvider
from .mock_provider import MockProvider
from .provider import SmallModelProvider


def get_provider(name: str | None = None) -> SmallModelProvider:
    provider_name = (name or getattr(settings, "SMALL_MODEL_PROVIDER", "mock") or "mock").strip().lower()
    if provider_name == "mock":
        return MockProvider()
    if provider_name == "gemini":
        return GeminiProvider()
    raise ValueError(f"Unknown small model provider: {provider_name}")
