"""
First-boot seed. Creates a default organization + workspace if none exist, so
the app boots into a working state without the operator running migrations
manually. The seed mirrors the existing `technodysis` YAML config to preserve
backward compatibility with the pre-DB refactor.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.database.engine import async_session_factory
from app.models import Organization, Workspace


log = logging.getLogger("db.seed")

DEFAULT_ORG_SLUG = "default"
DEFAULT_WORKSPACE_SLUG = "technodysis"


async def seed_defaults() -> None:
    async with async_session_factory() as session:
        # Skip if we already have data
        existing = (await session.execute(select(Organization).limit(1))).scalar_one_or_none()
        if existing is not None:
            return

        org = Organization(slug=DEFAULT_ORG_SLUG, name="Default")
        session.add(org)
        await session.flush()

        ws = Workspace(
            organization_id=org.id,
            slug=DEFAULT_WORKSPACE_SLUG,
            name="Technodysis",
        )
        # Try to import the existing YAML settings so the default workspace
        # behaves identically to the pre-DB setup.
        try:
            from app.tenant.registry import get_registry
            cfg = get_registry().get(DEFAULT_WORKSPACE_SLUG)
            ws.settings = {
                "llm": {
                    "provider": cfg.llm.provider, "model": cfg.llm.model,
                    "base_url": cfg.llm.base_url,
                    "temperature": cfg.llm.temperature,
                    "max_tokens": cfg.llm.max_tokens,
                    "context_window": cfg.llm.context_window,
                    "request_timeout": cfg.llm.request_timeout,
                    "chat_history_limit": cfg.llm.chat_history_limit,
                },
                "rag": {
                    "embedding_model": cfg.rag.embedding_model,
                    "chunk_size": cfg.rag.chunk_size,
                    "top_k": cfg.rag.top_k,
                    "fuzzy_match_threshold": cfg.rag.fuzzy_match_threshold,
                    "entity_corrections": cfg.rag.entity_corrections or {},
                },
                "tts": {"provider": cfg.tts.provider, "default_voice": cfg.tts.default_voice},
                "stt": {"model": cfg.stt.model, "language": cfg.stt.language},
                "prompt": {"template": cfg.prompt_template},
                "branding": {
                    "bot_name": cfg.display_name or "Assistant",
                    "primary_color": "#6aa5ff",
                    "welcome_message": "Hi! How can I help you today?",
                    "widget_position": "bottom-right",
                    "theme": "auto",
                },
                "features": cfg.features or {},
            }
        except Exception as e:
            log.warning(f"[seed] could not import YAML defaults: {e}")

        session.add(ws)
        await session.commit()
        log.info(f"[seed] created default org={org.slug} workspace={ws.slug}")
