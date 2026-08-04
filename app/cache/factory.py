"""Pick cache backend. Uses REDIS_URL when set; otherwise in-memory."""

from __future__ import annotations

import os
import threading
from typing import Optional

from app.cache.base import CacheBackend
from app.cache.memory import InMemoryCache


_backend: Optional[CacheBackend] = None
_lock = threading.Lock()


def get_cache() -> CacheBackend:
    global _backend
    if _backend is not None:
        return _backend
    with _lock:
        if _backend is not None:
            return _backend
        redis_url = os.getenv("REDIS_URL", "").strip()
        if redis_url:
            try:
                from app.cache.redis_backend import RedisCache
                _backend = RedisCache(redis_url)
                return _backend
            except Exception as e:
                import logging
                logging.getLogger("cache").warning(
                    f"[cache] REDIS_URL set but Redis unavailable ({e}); falling back to in-memory"
                )
        _backend = InMemoryCache()
        return _backend
