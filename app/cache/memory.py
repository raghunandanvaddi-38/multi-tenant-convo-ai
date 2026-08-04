"""In-process cache. Not shared across workers; fine for single-process dev."""

from __future__ import annotations

import asyncio
import time
from typing import Optional


class InMemoryCache:
    def __init__(self):
        self._store: dict[str, tuple[str, float]] = {}
        self._counters: dict[str, tuple[int, float]] = {}
        self._lock = asyncio.Lock()

    def _expired(self, expiry: float) -> bool:
        return expiry > 0 and time.time() > expiry

    async def get(self, key: str) -> Optional[str]:
        entry = self._store.get(key)
        if entry is None: return None
        val, exp = entry
        if self._expired(exp):
            self._store.pop(key, None); return None
        return val

    async def set(self, key: str, value: str, ttl_seconds: int = 0) -> None:
        exp = time.time() + ttl_seconds if ttl_seconds > 0 else 0
        self._store[key] = (value, exp)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def incr(self, key: str, ttl_seconds: int = 0) -> int:
        async with self._lock:
            now = time.time()
            entry = self._counters.get(key)
            if entry is None or (entry[1] > 0 and now > entry[1]):
                exp = now + ttl_seconds if ttl_seconds > 0 else 0
                self._counters[key] = (1, exp)
                return 1
            n, exp = entry
            n += 1
            self._counters[key] = (n, exp)
            return n
