"""
Backward-compat shim over the new multi-tenant service layer.

This module preserves the pre-refactor public API (chat, chat_stream, session
helpers, backchannels) so existing callers — routes.py, ws_output.py,
websocket.py — keep working. All real logic lives in:

    app.services.conversation_service   — orchestration
    app.gateway.ai_gateway              — LLM provider routing
    app.rag.tenant_rag                  — per-tenant retrieval
    app.services.memory_store           — conversation memory
    app.tenant.*                        — identity + config

New code should call those directly with a TenantContext. This shim
resolves to the default tenant when no context is supplied.
"""

from __future__ import annotations

import asyncio
import logging
import random
import subprocess
import time
from typing import AsyncIterator, Optional

import ollama
from langchain_core.messages import AIMessage, HumanMessage

from app.core.voices import detect_tts_provider_from_voice
from app.services.conversation_service import get_conversation_service
from app.services.memory_store import get_memory_store
from app.tenant.context import TenantContext
from app.tenant.middleware import DEFAULT_TENANT_ID
from app.tenant.registry import get_registry


log = logging.getLogger("agent")

_BACKCHANNELS = ["Okay…", "Got it…", "Alright…", "I see…", "Sure…"]

_system_ready = False
_init_lock = asyncio.Lock()


# --- Session context resolution ---------------------------------------
# Callers pass `session_id` (from the WS handshake). We keep a lightweight
# mapping so a session_id resolves to a TenantContext for the default tenant.
# WebSocket code that already knows the tenant should pass a real
# TenantContext directly via the new service layer.

_session_contexts: dict[str, TenantContext] = {}


def bind_session(session_id: str, ctx: TenantContext) -> None:
    """Called by the WS layer after tenant handshake."""
    _session_contexts[session_id] = ctx


def _ctx_for_session(session_id: str) -> TenantContext:
    ctx = _session_contexts.get(session_id)
    if ctx is not None:
        return ctx
    # Default tenant fallback — preserves old single-tenant behaviour.
    registry = get_registry()
    ctx = TenantContext(
        tenant_id=DEFAULT_TENANT_ID,
        user_id="anonymous",
        conversation_id=session_id,
        config=registry.get(DEFAULT_TENANT_ID),
    )
    _session_contexts[session_id] = ctx
    return ctx


# --- Legacy startup (Ollama server bootstrap + warmup) ----------------

def LLM_MODEL_initialization():
    try:
        ollama.list()
        log.info("[agent] Ollama already running")
    except Exception:
        log.info("[agent] Starting Ollama server...")
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(5)


async def ensure_system_initialized():
    """Preload the default tenant's RAG index + warm the default LLM."""
    global _system_ready
    if _system_ready:
        return

    async with _init_lock:
        if _system_ready:
            return

        LLM_MODEL_initialization()

        registry = get_registry()
        default_cfg = registry.get(DEFAULT_TENANT_ID)
        default_ctx = TenantContext(
            tenant_id=DEFAULT_TENANT_ID,
            user_id="system",
            conversation_id="startup",
            config=default_cfg,
        )

        service = get_conversation_service()

        # RAG preload
        try:
            await service.rag.ensure_ready(default_ctx)
        except Exception as e:
            log.warning(f"[agent] RAG preload failed for default tenant: {e}")

        # LLM warmup
        try:
            await service.gateway.warmup(default_ctx)
        except Exception as e:
            log.warning(f"[agent] LLM warmup failed: {e}")

        _system_ready = True
        log.info("[agent] System ready (default tenant)")


# --- Public streaming API (unchanged signatures) ----------------------

async def chat_stream(user_text: str, session_id: str) -> AsyncIterator[str]:
    yield " "
    ctx = _ctx_for_session(session_id)
    service = get_conversation_service()
    asyncio.ensure_future(_classify_and_store(user_text, session_id))
    async for token in service.stream(ctx, user_text):
        yield token


async def chat(user_text: str, session_id: str) -> str:
    parts = []
    async for token in chat_stream(user_text, session_id):
        parts.append(token)
    return "".join(parts).strip()


# --- Session helpers (delegate to memory store) -----------------------

def _mem_key(session_id: str) -> str:
    return _ctx_for_session(session_id).memory_key


def get_session_state(session_id: str) -> dict:
    mem = get_memory_store().get(_mem_key(session_id))
    return {
        "intent": mem["intent"],
        "emotion": mem["emotion"],
        "turns": len([m for m in mem["history"] if isinstance(m, HumanMessage)]),
        "voice": mem["voice"],
        "tts_provider": mem["tts_provider"],
    }


def update_session_tts(session_id: str, provider_or_voice: str, voice: Optional[str] = None) -> None:
    """
    Backward-compat: routes.py calls update_session_tts(session_id, voice).
    ws code calls update_session_tts(session_id, provider, voice).
    We accept both by treating a missing second arg as voice-only.
    """
    mem = get_memory_store().get(_mem_key(session_id))
    if voice is None:
        # single-arg form: (session_id, voice)
        voice_val = provider_or_voice
        mem["voice"] = voice_val
        if not mem.get("tts_provider"):
            detected = detect_tts_provider_from_voice(voice_val)
            mem["tts_provider"] = detected or mem.get("tts_provider", "")
    else:
        mem["tts_provider"] = provider_or_voice
        mem["voice"] = voice


def update_session_voice(session_id: str, voice: str) -> None:
    update_session_tts(session_id, voice)


def get_conversation_history(session_id: str) -> list[dict]:
    mem = get_memory_store().get(_mem_key(session_id))
    result = []
    for msg in mem["history"]:
        if isinstance(msg, HumanMessage):
            result.append({"role": "user", "content": msg.content})
        elif isinstance(msg, AIMessage):
            result.append({"role": "assistant", "content": msg.content})
    return result


def get_last_assistant_response(session_id: str) -> str:
    mem = get_memory_store().get(_mem_key(session_id))
    for msg in reversed(mem["history"]):
        if isinstance(msg, AIMessage) and msg.content.strip():
            return msg.content.strip()
    return ""


def truncate_last_assistant(session_id: str, spoken_text: str) -> None:
    get_memory_store().truncate_last_assistant(_mem_key(session_id), spoken_text)


def clear_session(session_id: str) -> None:
    key = _mem_key(session_id)
    get_memory_store().clear(key)
    _session_contexts.pop(session_id, None)


# --- Backchannels -----------------------------------------------------

def get_backchannel(session_id: str) -> str:
    ctx = _ctx_for_session(session_id)
    from app.config import BC_COOLDOWN

    store = get_memory_store()
    now = time.monotonic()
    if now - store.bc_last(ctx.memory_key) < BC_COOLDOWN:
        return ""
    store.bc_touch(ctx.memory_key, now)
    return random.choice(_BACKCHANNELS)


# --- Lightweight intent/emotion classifier (unchanged behaviour) ------

async def _classify_and_store(user_text: str, session_id: str) -> None:
    text = user_text.lower()
    emotion = "neutral"
    intent = "information_request"

    if any(x in text for x in ["hi", "hello", "hey"]):
        intent = "greeting"
    elif any(x in text for x in ["bye", "goodbye"]):
        intent = "goodbye"
    elif any(x in text for x in ["problem", "issue", "not working"]):
        intent = "complaint"

    if any(x in text for x in ["angry", "bad", "worst"]):
        emotion = "angry"
    elif any(x in text for x in ["happy", "great", "awesome"]):
        emotion = "happy"

    mem = get_memory_store().get(_mem_key(session_id))
    mem["intent"] = intent
    mem["emotion"] = emotion
