"""
TenantRegistry — loads tenant configuration from config/tenants/*.yaml.

One YAML file per tenant. The registry is a process-level singleton but
tenant configs are immutable dataclasses returned to callers. Configs are
loaded lazily on first access and cached; call reload() to pick up edits.
"""

from __future__ import annotations

import os
import threading
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml


log = logging.getLogger("tenant.registry")


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model: str
    base_url: str = "http://localhost:11434"
    temperature: float = 0.1
    max_tokens: int = 150
    context_window: int = 2048
    request_timeout: int = 30
    api_timeout: int = 10
    warmup_timeout: int = 15
    warmup_num_predict: int = 5
    chat_history_limit: int = 5


@dataclass(frozen=True)
class RAGSettings:
    embedding_model: str
    knowledge_dir: str
    index_path: str
    chunks_path: str
    chunk_size: int = 150
    top_k: int = 2
    fuzzy_match_threshold: int = 80
    entity_corrections: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TTSSettings:
    provider: str = "kokoro"
    default_voice: str = "af_heart"
    fallback_provider: str = "edge"


@dataclass(frozen=True)
class STTSettings:
    model: str = "Qwen/Qwen3-ASR-1.7B"
    language: str = "English"


@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    display_name: str
    api_key_hash: Optional[str]
    prompt_template: str
    llm: LLMSettings
    rag: RAGSettings
    tts: TTSSettings
    stt: STTSettings
    features: dict = field(default_factory=dict)


def _tenants_dir() -> str:
    base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(base, "config", "tenants")


class TenantRegistry:
    """Process-level registry of TenantConfig objects, keyed by tenant_id."""

    def __init__(self, tenants_dir: Optional[str] = None):
        self._dir = tenants_dir or _tenants_dir()
        self._cache: dict[str, TenantConfig] = {}
        self._lock = threading.Lock()

    def _path(self, tenant_id: str) -> str:
        return os.path.join(self._dir, f"{tenant_id}.yaml")

    def _load(self, tenant_id: str) -> TenantConfig:
        path = self._path(tenant_id)
        if not os.path.isfile(path):
            raise KeyError(f"Unknown tenant: {tenant_id!r} (looked in {self._dir})")

        with open(path, "r", encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}

        return TenantConfig(
            tenant_id=data.get("tenant_id", tenant_id),
            display_name=data.get("display_name", tenant_id),
            api_key_hash=data.get("api_key_hash"),
            prompt_template=data.get("prompt_template", ""),
            llm=LLMSettings(**(data.get("llm") or {})),
            rag=RAGSettings(**(data.get("rag") or {})),
            tts=TTSSettings(**(data.get("tts") or {})),
            stt=STTSettings(**(data.get("stt") or {})),
            features=data.get("features") or {},
        )

    def get(self, tenant_id: str) -> TenantConfig:
        cfg = self._cache.get(tenant_id)
        if cfg is not None:
            return cfg
        with self._lock:
            cfg = self._cache.get(tenant_id)
            if cfg is not None:
                return cfg
            cfg = self._load(tenant_id)
            self._cache[tenant_id] = cfg
            log.info(f"[tenant] loaded config for {tenant_id!r}")
            return cfg

    def has(self, tenant_id: str) -> bool:
        if tenant_id in self._cache:
            return True
        return os.path.isfile(self._path(tenant_id))

    def reload(self, tenant_id: Optional[str] = None) -> None:
        with self._lock:
            if tenant_id is None:
                self._cache.clear()
            else:
                self._cache.pop(tenant_id, None)

    def list_tenants(self) -> list[str]:
        if not os.path.isdir(self._dir):
            return []
        return sorted(
            os.path.splitext(f)[0]
            for f in os.listdir(self._dir)
            if f.endswith(".yaml")
        )


_registry: Optional[TenantRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> TenantRegistry:
    global _registry
    if _registry is not None:
        return _registry
    with _registry_lock:
        if _registry is None:
            _registry = TenantRegistry()
        return _registry
