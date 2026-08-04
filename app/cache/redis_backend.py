"""Redis-backed cache. Loaded only when REDIS_URL is set."""

from __future__ import annotations

from typing import Optional


class RedisCache:
    def __init__(self, url: str):
        import redis.asyncio as redis
        self._r = redis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> Optional[str]:
        return await self._r.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int = 0) -> None:
        if ttl_seconds > 0:
            await self._r.setex(key, ttl_seconds, value)
        else:
            await self._r.set(key, value)

    async def delete(self, key: str) -> None:
        await self._r.delete(key)

    async def incr(self, key: str, ttl_seconds: int = 0) -> int:
        pipe = self._r.pipeline()
        pipe.incr(key)
        if ttl_seconds > 0:
            pipe.expire(key, ttl_seconds, nx=True)
        result = await pipe.execute()
        return int(result[0])
