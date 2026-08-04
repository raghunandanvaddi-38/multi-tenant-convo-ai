"""
ProviderRouter — resolves a TenantContext to a concrete LLMProvider.

Adding a new provider is a two-line change here plus a new provider class.
Providers are cached per (name, base_url) so we don't rebuild them per request.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from app.providers.base import LLMProvider
from app.providers.ollama_provider import OllamaProvider
from app.tenant.registry import LLMSettings


log = logging.getLogger("gateway.router")


ProviderFactory = Callable[[LLMSettings], LLMProvider]


def _build_ollama(cfg: LLMSettings) -> LLMProvider:
    return OllamaProvider(base_url=cfg.base_url, api_timeout=cfg.api_timeout)


class ProviderRouter:
    def __init__(self):
        self._factories: dict[str, ProviderFactory] = {
            "ollama": _build_ollama,
        }
        self._cache: dict[tuple[str, str], LLMProvider] = {}
        self._lock = threading.Lock()

    def register(self, name: str, factory: ProviderFactory) -> None:
        self._factories[name] = factory

    def resolve(self, cfg: LLMSettings) -> LLMProvider:
        key = (cfg.provider, cfg.base_url)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            factory = self._factories.get(cfg.provider)
            if factory is None:
                raise ValueError(f"Unknown LLM provider: {cfg.provider!r}")
            provider = factory(cfg)
            self._cache[key] = provider
            log.info(f"[gateway] provider {cfg.provider!r} ready (base={cfg.base_url})")
            return provider
