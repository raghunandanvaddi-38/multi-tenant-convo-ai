"""Cache interface + implementations (in-memory now, Redis via env swap)."""

from app.cache.base import CacheBackend
from app.cache.memory import InMemoryCache
from app.cache.factory import get_cache

__all__ = ["CacheBackend", "InMemoryCache", "get_cache"]
