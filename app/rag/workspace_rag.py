"""
WorkspaceRAGService — per-workspace FAISS index + embedding model cache.

Data layout on disk (LocalDiskBackend at STORAGE_ROOT):
  <root>/workspaces/<workspace_id>/index.faiss
  <root>/workspaces/<workspace_id>/chunks.npy

Embedding models are shared across workspaces that use the same model name.
Indexes are lazily loaded and cached per workspace_id.

The service also supports incremental ingest — add_chunks() extends an
existing index rather than rebuilding it. Used by the Phase 4 pipeline.
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

from app.storage import get_storage
from app.workspaces.context import WorkspaceContext


log = logging.getLogger("rag.workspace")


@dataclass
class _Index:
    index: faiss.Index
    chunks: list[str]
    dim: int
    embed_model_name: str


class _EmbedCache:
    def __init__(self):
        self._models: dict[str, object] = {}
        self._lock = threading.Lock()

    def get(self, name: str):
        cached = self._models.get(name)
        if cached is not None: return cached
        with self._lock:
            cached = self._models.get(name)
            if cached is not None: return cached
            from sentence_transformers import SentenceTransformer
            log.info(f"[rag] loading embedding model {name!r}")
            m = SentenceTransformer(name)
            self._models[name] = m
            return m


class WorkspaceRAGService:
    def __init__(self):
        self._indexes: dict[str, _Index] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = threading.Lock()
        self._embed_cache = _EmbedCache()

    def _lock_for(self, wid: str) -> asyncio.Lock:
        lock = self._locks.get(wid)
        if lock is not None: return lock
        with self._locks_guard:
            lock = self._locks.get(wid)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[wid] = lock
            return lock

    def _paths(self, workspace_id: str) -> tuple[str, str]:
        prefix = f"workspaces/{workspace_id}"
        return f"{prefix}/index.faiss", f"{prefix}/chunks.npy"

    def _abs(self, storage, rel: str) -> str:
        # LocalDiskBackend exposes .root; other backends would need staging.
        return storage._resolve(rel)  # type: ignore[attr-defined]

    def _load_from_disk_sync(self, ctx: WorkspaceContext) -> Optional[_Index]:
        storage = get_storage()
        index_rel, chunks_rel = self._paths(ctx.workspace_id)
        try:
            idx_path = self._abs(storage, index_rel)
            chunks_path = self._abs(storage, chunks_rel)
        except Exception:
            return None
        if not (os.path.exists(idx_path) and os.path.exists(chunks_path)):
            return None
        try:
            index = faiss.read_index(idx_path)
            chunks = list(np.load(chunks_path, allow_pickle=True))
            embed_model_name = ctx.settings.rag.get("embedding_model", "all-MiniLM-L6-v2")
            return _Index(index=index, chunks=chunks, dim=index.d, embed_model_name=embed_model_name)
        except Exception as e:
            log.warning(f"[rag] failed to load workspace={ctx.workspace_id} index: {e}")
            return None

    def _persist_sync(self, workspace_id: str, idx: _Index) -> None:
        storage = get_storage()
        index_rel, chunks_rel = self._paths(workspace_id)
        idx_path = self._abs(storage, index_rel)
        chunks_path = self._abs(storage, chunks_rel)
        os.makedirs(os.path.dirname(idx_path), exist_ok=True)
        faiss.write_index(idx.index, idx_path)
        np.save(chunks_path, np.array(idx.chunks, dtype=object))

    def _new_index(self, dim: int, embed_model_name: str) -> _Index:
        return _Index(index=faiss.IndexFlatIP(dim), chunks=[], dim=dim, embed_model_name=embed_model_name)

    async def ensure_ready(self, ctx: WorkspaceContext) -> _Index:
        cached = self._indexes.get(ctx.workspace_id)
        if cached is not None:
            return cached
        async with self._lock_for(ctx.workspace_id):
            cached = self._indexes.get(ctx.workspace_id)
            if cached is not None:
                return cached
            loaded = await asyncio.to_thread(self._load_from_disk_sync, ctx)
            if loaded is not None:
                self._indexes[ctx.workspace_id] = loaded
                return loaded
            # No index yet — create an empty one dimensioned by the embed model
            embed_name = ctx.settings.rag.get("embedding_model", "all-MiniLM-L6-v2")
            embed_model = self._embed_cache.get(embed_name)
            probe = await asyncio.to_thread(
                embed_model.encode, ["x"], normalize_embeddings=True
            )
            dim = int(np.asarray(probe).shape[1])
            fresh = self._new_index(dim, embed_name)
            self._indexes[ctx.workspace_id] = fresh
            return fresh

    async def add_chunks(self, ctx: WorkspaceContext, chunks: list[str]) -> int:
        """Append chunks to the workspace index. Returns count added."""
        if not chunks: return 0
        state = await self.ensure_ready(ctx)
        embed_model = self._embed_cache.get(state.embed_model_name)

        def _run():
            embeddings = embed_model.encode(chunks, normalize_embeddings=True).astype("float32")
            state.index.add(embeddings)
            state.chunks.extend(chunks)
            self._persist_sync(ctx.workspace_id, state)
            return len(chunks)

        async with self._lock_for(ctx.workspace_id):
            return await asyncio.to_thread(_run)

    def _normalize_query(self, query: str, cfg: dict) -> str:
        q = query.lower().strip()
        q = re.sub(r"[^\w\s]", "", q)
        threshold = int(cfg.get("fuzzy_match_threshold", 80))
        for correct, mistakes in (cfg.get("entity_corrections") or {}).items():
            for mistake in mistakes:
                if fuzz.ratio(q, mistake) >= threshold:
                    q = q.replace(mistake, correct)
        return q

    async def retrieve(self, ctx: WorkspaceContext, query: str) -> list[str]:
        cfg = ctx.settings.rag
        state = await self.ensure_ready(ctx)
        if state.index.ntotal == 0:
            return []
        clean = self._normalize_query(query, cfg)
        embed_model = self._embed_cache.get(state.embed_model_name)
        top_k = int(cfg.get("top_k", 2))

        def _search():
            q = embed_model.encode([clean], normalize_embeddings=True).astype("float32")
            _, idx = state.index.search(q, min(top_k, state.index.ntotal))
            return [state.chunks[i] for i in idx[0] if 0 <= i < len(state.chunks)]

        return await asyncio.to_thread(_search)

    def invalidate(self, workspace_id: str) -> None:
        """Drop the in-memory index; next request rebuilds from disk."""
        self._indexes.pop(workspace_id, None)


_service: Optional[WorkspaceRAGService] = None
_service_lock = threading.Lock()


def get_workspace_rag() -> WorkspaceRAGService:
    global _service
    if _service is not None: return _service
    with _service_lock:
        if _service is None:
            _service = WorkspaceRAGService()
        return _service
