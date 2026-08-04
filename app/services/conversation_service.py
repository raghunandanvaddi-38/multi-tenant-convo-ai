"""
ConversationService — the orchestrator that binds tenant + RAG + gateway + memory.

Callers pass a TenantContext and user text; the service handles prompt
assembly, memory updates, retrieval, and streaming. This is the layer the
WebSocket pipeline and REST endpoints should depend on.
"""

from __future__ import annotations

import logging
import threading
from typing import AsyncIterator, Optional

from langchain_core.messages import AIMessage, HumanMessage

from app.gateway.ai_gateway import AIGateway, get_gateway
from app.rag.tenant_rag import TenantRAGService, get_rag_service
from app.rag.workspace_rag import WorkspaceRAGService, get_workspace_rag
from app.services.memory_store import MemoryStore, get_memory_store
from app.tenant.context import TenantContext
from app.workspaces.context import WorkspaceContext


log = logging.getLogger("conversation")


class ConversationService:
    def __init__(
        self,
        gateway: Optional[AIGateway] = None,
        rag=None,                                    # legacy TenantRAGService (or a mock)
        workspace_rag: Optional[WorkspaceRAGService] = None,
        memory: Optional[MemoryStore] = None,
    ):
        self._gateway = gateway or get_gateway()
        self._rag = rag or get_rag_service()
        self._workspace_rag = workspace_rag or get_workspace_rag()
        self._memory = memory or get_memory_store()

    @property
    def gateway(self) -> AIGateway: return self._gateway

    @property
    def rag(self): return self._rag

    @property
    def workspace_rag(self) -> WorkspaceRAGService: return self._workspace_rag

    @property
    def memory(self) -> MemoryStore: return self._memory

    def _rag_for(self, ctx):
        """Dispatch: WorkspaceContext → DB-backed RAG, TenantContext → YAML RAG."""
        return self._workspace_rag if isinstance(ctx, WorkspaceContext) else self._rag

    def _history_text(self, ctx) -> str:
        # Works with both TenantContext (dataclass) and WorkspaceContext (dict shim).
        limit = getattr(ctx.config.llm, "chat_history_limit", 5) or 5
        history = self._memory.get(ctx.memory_key)["history"]
        entries = []
        for msg in history[-(limit * 2):]:
            if isinstance(msg, HumanMessage) and msg.content:
                entries.append(f"User: {msg.content}")
            elif isinstance(msg, AIMessage) and msg.content:
                entries.append(f"Assistant: {msg.content}")
        return "\n".join(entries[-limit:])

    def _cache_key(self, ctx: TenantContext, text: str) -> str:
        return f"{ctx.tenant_id}:{ctx.memory_key}:{text.strip().lower()}"

    async def stream(self, ctx: TenantContext, user_text: str) -> AsyncIterator[str]:
        log.debug(
            "[conversation] tenant=%s conv=%s user=%s len=%d",
            ctx.tenant_id, ctx.conversation_id, ctx.user_id, len(user_text),
        )
        self._memory.append_user(ctx.memory_key, user_text)

        # Response cache (per-tenant, per-conversation)
        cache_key = self._cache_key(ctx, user_text)
        cached = self._memory.cache_get(cache_key)
        if cached is not None:
            for word in cached.split():
                yield word + " "
            self._memory.append_assistant(ctx.memory_key, cached)
            return

        top_chunks = await self._rag_for(ctx).retrieve(ctx, user_text)
        context_str = "\n\n".join(str(c) for c in top_chunks)
        conversation_history = self._history_text(ctx)

        prompt = ctx.config.prompt_template.format(
            conversation_history=conversation_history,
            context=context_str,
            query=user_text,
        )

        full = ""
        try:
            async for token in self._gateway.generate_stream(ctx, prompt):
                full += token
                yield token
        except Exception as e:
            provider = getattr(ctx.config.llm, "provider", "?")
            log.error(
                "[conversation] tenant=%s provider=%s error=%s",
                ctx.tenant_id, provider, e,
            )
            fallback = "Sorry, something went wrong."
            yield fallback
            self._memory.append_assistant(ctx.memory_key, fallback)
            return

        self._memory.cache_set(cache_key, full)
        self._memory.append_assistant(ctx.memory_key, full)


_service: Optional[ConversationService] = None
_service_lock = threading.Lock()


def get_conversation_service() -> ConversationService:
    global _service
    if _service is not None:
        return _service
    with _service_lock:
        if _service is None:
            _service = ConversationService()
        return _service
