"""
TenantRAGService — one FAISS index + embedding model + chunks per tenant.

Tenants are lazily initialized on first retrieval and cached. Different
tenants may use different embedding models — each model is loaded once
and shared across tenants that reference it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Optional

import faiss
import numpy as np
from rapidfuzz import fuzz

from app.rag.loader import load_directory, split_text
from app.tenant.context import TenantContext
from app.tenant.registry import RAGSettings


log = logging.getLogger("rag")


@dataclass
class _TenantIndex:
    index: faiss.Index
    chunks: np.ndarray
    embed_model_name: str


class _EmbedCache:
    """Shared embedding model cache — one SentenceTransformer per model name."""

    def __init__(self):
        self._models: dict[str, object] = {}
        self._lock = threading.Lock()

    def get(self, model_name: str):
        cached = self._models.get(model_name)
        if cached is not None:
            return cached
        with self._lock:
            cached = self._models.get(model_name)
            if cached is not None:
                return cached
            from sentence_transformers import SentenceTransformer
            log.info(f"[rag] loading embedding model {model_name!r}")
            model = SentenceTransformer(model_name)
            self._models[model_name] = model
            return model


class TenantRAGService:
    def __init__(self):
        self._indexes: dict[str, _TenantIndex] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = threading.Lock()
        self._embed_cache = _EmbedCache()

    def _lock_for(self, tenant_id: str) -> asyncio.Lock:
        lock = self._locks.get(tenant_id)
        if lock is not None:
            return lock
        with self._locks_guard:
            lock = self._locks.get(tenant_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[tenant_id] = lock
            return lock

    def _build_sync(self, cfg: RAGSettings) -> _TenantIndex:
        os.makedirs(os.path.dirname(cfg.index_path), exist_ok=True)

        embed_model = self._embed_cache.get(cfg.embedding_model)

        if os.path.exists(cfg.index_path) and os.path.exists(cfg.chunks_path):
            log.info(f"[rag] loading existing index from {cfg.index_path}")
            index = faiss.read_index(cfg.index_path)
            chunks = np.load(cfg.chunks_path, allow_pickle=True)
            return _TenantIndex(index=index, chunks=chunks, embed_model_name=cfg.embedding_model)

        log.info(f"[rag] building new index from {cfg.knowledge_dir}")
        text = load_directory(cfg.knowledge_dir)
        if not text.strip():
            raise ValueError(f"[rag] no readable content in {cfg.knowledge_dir}")

        chunks_list = split_text(text, cfg.chunk_size)
        embeddings = embed_model.encode(chunks_list, normalize_embeddings=True).astype("float32")
        dim = embeddings.shape[1]

        index = faiss.IndexFlatIP(dim)
        index.add(embeddings)

        faiss.write_index(index, cfg.index_path)
        chunks_arr = np.array(chunks_list, dtype=object)
        np.save(cfg.chunks_path, chunks_arr)
        return _TenantIndex(index=index, chunks=chunks_arr, embed_model_name=cfg.embedding_model)

    async def ensure_ready(self, ctx: TenantContext) -> _TenantIndex:
        cached = self._indexes.get(ctx.tenant_id)
        if cached is not None:
            return cached
        async with self._lock_for(ctx.tenant_id):
            cached = self._indexes.get(ctx.tenant_id)
            if cached is not None:
                return cached
            built = await asyncio.to_thread(self._build_sync, ctx.config.rag)
            self._indexes[ctx.tenant_id] = built
            return built

    def _normalize_query(self, query: str, cfg: RAGSettings) -> str:
        q = query.lower().strip()
        q = re.sub(r"[^\w\s]", "", q)
        for correct, mistakes in (cfg.entity_corrections or {}).items():
            for mistake in mistakes:
                if fuzz.ratio(q, mistake) >= cfg.fuzzy_match_threshold:
                    q = q.replace(mistake, correct)
        return q

    async def retrieve(self, ctx: TenantContext, query: str) -> list[str]:
        cfg = ctx.config.rag
        state = await self.ensure_ready(ctx)
        clean = self._normalize_query(query, cfg)
        embed_model = self._embed_cache.get(state.embed_model_name)

        def _search():
            q = embed_model.encode([clean], normalize_embeddings=True).astype("float32")
            _, idx = state.index.search(q, cfg.top_k)
            return [state.chunks[i] for i in idx[0]]

        return await asyncio.to_thread(_search)


_service: Optional[TenantRAGService] = None
_service_lock = threading.Lock()


def get_rag_service() -> TenantRAGService:
    global _service
    if _service is not None:
        return _service
    with _service_lock:
        if _service is None:
            _service = TenantRAGService()
        return _service
