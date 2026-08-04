"""
Per-API-key rate limiter. Sliding-second window with a per-minute quota.

Only enforced on /v1/chat* and /v1/documents* — management routes are protected
by JWT and by definition low-volume. Uses the cache backend (in-mem or Redis)
so it can span workers when Redis is configured.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.cache import get_cache


log = logging.getLogger("middleware.ratelimit")

# Sensible defaults; override with env
REQUESTS_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MIN", "60"))
BURST_PER_SECOND = int(os.getenv("RATE_LIMIT_BURST", "10"))


PROTECTED_PREFIXES = ("/v1/chat", "/v1/documents", "/v1/stt", "/v1/tts")


def _key_from_request(request: Request) -> str | None:
    api_key = request.headers.get("x-api-key")
    if not api_key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(None, 1)[1].strip()
            if token.startswith("sk_"):
                api_key = token
    if api_key and api_key.startswith("sk_"):
        parts = api_key.split("_", 2)
        return parts[1] if len(parts) >= 2 else api_key[:8]
    return None


class APIKeyRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        path = request.url.path
        if not any(path.startswith(p) for p in PROTECTED_PREFIXES):
            return await call_next(request)

        prefix = _key_from_request(request)
        if prefix is None:
            # Let the route's auth dep return 401 with a clean error.
            return await call_next(request)

        cache = get_cache()
        try:
            per_sec = await cache.incr(f"rl:sec:{prefix}", ttl_seconds=1)
            per_min = await cache.incr(f"rl:min:{prefix}", ttl_seconds=60)
        except Exception as e:
            log.warning(f"[ratelimit] cache error, allowing through: {e}")
            return await call_next(request)

        if per_sec > BURST_PER_SECOND or per_min > REQUESTS_PER_MINUTE:
            return JSONResponse(
                {"error": "rate limit exceeded", "retry_after_seconds": 1 if per_sec > BURST_PER_SECOND else 60},
                status_code=429,
                headers={"retry-after": "1" if per_sec > BURST_PER_SECOND else "60"},
            )
        return await call_next(request)
