"""
Async SQLAlchemy engine + session factory.

DATABASE_URL env var picks the backend:
  - sqlite+aiosqlite:///./data/platform.db   (default — no external deps)
  - postgresql+asyncpg://user:pass@host/db   (production)

All models inherit from `Base`. `init_db()` runs at startup to (a) create
tables in dev without Alembic and (b) seed the default organization + workspace
that mirrors the existing `technodysis` YAML so backward compatibility holds.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import DateTime, String, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


log = logging.getLogger("db")


def _default_db_url() -> str:
    # Keep local data alongside existing knowledge base
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    data_dir = os.path.join(base, "data")
    os.makedirs(data_dir, exist_ok=True)
    return f"sqlite+aiosqlite:///{os.path.join(data_dir, 'platform.db')}"


DATABASE_URL = os.getenv("DATABASE_URL", _default_db_url())
ECHO_SQL = os.getenv("DB_ECHO", "0") == "1"


def _new_id() -> str:
    """Compact 32-char id, sortable by generation time (uuid4 hex)."""
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base with id + timestamps for every table."""

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=ECHO_SQL,
    pool_pre_ping=True,
    # SQLite likes only one writer at a time; async driver uses one connection.
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

# SQLite needs foreign keys turned on per connection.
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency — one session per request, rolled back on exception."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create tables + seed the default org/workspace on first boot."""
    # Import all model modules so their tables are registered on Base.metadata.
    from app.models import (  # noqa: F401
        user, organization, workspace, membership, api_key, document, prompt_version, event,
        db_connection,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from app.database.seed import seed_defaults
    await seed_defaults()
    log.info(f"[db] initialized ({DATABASE_URL.split('://', 1)[0]})")
