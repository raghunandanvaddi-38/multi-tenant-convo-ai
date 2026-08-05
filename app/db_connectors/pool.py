"""
Per-connection engine cache. SQLAlchemy async engines have their own pooling
(default 5 connections + overflow); we just keep one engine per spec_key so
we don't rebuild it on every request.

The key includes db_type + host + port + database + user, but NOT the password.
If a user rotates the password the engine will start failing — the API layer
detects this on the next `test` call and rebuilds via `invalidate()`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db_connectors.base import ConnectionSpec


log = logging.getLogger("db_connectors.pool")


def spec_key(spec: ConnectionSpec) -> str:
    return f"{spec.db_type}://{spec.username}@{spec.host}:{spec.port}/{spec.database}"


def build_url(spec: ConnectionSpec) -> str:
    driver = {
        "postgres": "postgresql+asyncpg",
        "mysql": "mysql+aiomysql",
    }.get(spec.db_type)
    if driver is None:
        raise ValueError(f"Unknown db_type: {spec.db_type}")

    from urllib.parse import quote_plus
    user = quote_plus(spec.username)
    pw = quote_plus(spec.password or "")
    host = spec.host
    port = spec.port
    db = quote_plus(spec.database)
    url = f"{driver}://{user}:{pw}@{host}:{port}/{db}"

    # asyncpg accepts ssl=true, aiomysql accepts ssl={"ssl":true}. We simplify:
    if spec.ssl_enabled and spec.db_type == "postgres":
        url += "?ssl=require"
    return url


class EnginePool:
    def __init__(self):
        self._engines: dict[str, AsyncEngine] = {}
        self._lock = asyncio.Lock()

    async def get(self, spec: ConnectionSpec) -> AsyncEngine:
        key = spec_key(spec)
        eng = self._engines.get(key)
        if eng is not None:
            return eng
        async with self._lock:
            eng = self._engines.get(key)
            if eng is not None:
                return eng
            url = build_url(spec)
            connect_args = {}
            if spec.db_type == "mysql" and spec.ssl_enabled:
                connect_args["ssl"] = {"ssl": True}
            eng = create_async_engine(
                url,
                pool_size=5, max_overflow=5, pool_pre_ping=True, pool_recycle=1800,
                connect_args=connect_args,
                # We NEVER want to log queries containing credentials — off by default.
                echo=False,
            )
            self._engines[key] = eng
            log.info(f"[db-pool] engine ready for {key}")
            return eng

    async def invalidate(self, key: str) -> None:
        async with self._lock:
            eng = self._engines.pop(key, None)
        if eng is not None:
            try:
                await eng.dispose()
            except Exception as e:
                log.warning(f"[db-pool] dispose {key} failed: {e}")


_pool = EnginePool()


def engine_pool() -> EnginePool:
    return _pool
