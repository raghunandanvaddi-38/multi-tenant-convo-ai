"""
MemoryStore — conversation state keyed by (tenant_id, conversation_id).

In-process implementation. Swap for Redis by implementing the same interface
and returning it from get_memory_store().
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Optional, TypedDict

from langchain_core.messages import AIMessage, HumanMessage


class SessionMemory(TypedDict):
    intent: str
    emotion: str
    history: list
    voice: str
    tts_provider: str


class MemoryStore:
    def __init__(self, max_history_turns: int = 20, response_cache_size: int = 100):
        self._memories: dict[str, SessionMemory] = {}
        self._response_cache: OrderedDict[str, str] = OrderedDict()
        self._last_bc_time: dict[str, float] = {}
        self._lock = threading.Lock()
        self._max_history_turns = max_history_turns
        self._response_cache_size = response_cache_size

    def _new_memory(self) -> SessionMemory:
        return {
            "intent": "information_request",
            "emotion": "neutral",
            "history": [],
            "voice": "",
            "tts_provider": "",
        }

    def get(self, key: str) -> SessionMemory:
        mem = self._memories.get(key)
        if mem is not None:
            return mem
        with self._lock:
            mem = self._memories.get(key)
            if mem is None:
                mem = self._new_memory()
                self._memories[key] = mem
            return mem

    def clear(self, key: str) -> None:
        with self._lock:
            self._memories.pop(key, None)
            self._last_bc_time.pop(key, None)

    def append_user(self, key: str, text: str) -> None:
        mem = self.get(key)
        mem["history"].append(HumanMessage(content=text))
        self._prune(mem)

    def append_assistant(self, key: str, text: str) -> None:
        mem = self.get(key)
        mem["history"].append(AIMessage(content=text))
        self._prune(mem)

    def truncate_last_assistant(self, key: str, spoken_text: str) -> None:
        history = self.get(key)["history"]
        if history and isinstance(history[-1], AIMessage):
            history[-1] = AIMessage(content=spoken_text.strip())

    def _prune(self, mem: SessionMemory) -> None:
        limit = self._max_history_turns * 2
        if len(mem["history"]) > limit:
            mem["history"] = mem["history"][-limit:]

    # Response cache -----------------------------------------------------
    def cache_get(self, key: str) -> Optional[str]:
        if key in self._response_cache:
            self._response_cache.move_to_end(key)
            return self._response_cache[key]
        return None

    def cache_set(self, key: str, value: str) -> None:
        if key in self._response_cache:
            self._response_cache.move_to_end(key)
        self._response_cache[key] = value
        if len(self._response_cache) > self._response_cache_size:
            self._response_cache.popitem(last=False)

    # Backchannel throttle ----------------------------------------------
    def bc_last(self, key: str) -> float:
        return self._last_bc_time.get(key, 0.0)

    def bc_touch(self, key: str, now: float) -> None:
        self._last_bc_time[key] = now


_store: Optional[MemoryStore] = None
_store_lock = threading.Lock()


def get_memory_store() -> MemoryStore:
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is None:
            _store = MemoryStore()
        return _store
