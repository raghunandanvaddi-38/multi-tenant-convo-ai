from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.engine import Base


DEFAULT_SETTINGS: dict[str, Any] = {
    "llm": {
        "provider": "ollama",
        "model": "llama3.2:3b",
        "base_url": "http://localhost:11434",
        "temperature": 0.1,
        "max_tokens": 150,
        "context_window": 2048,
        "request_timeout": 30,
        "chat_history_limit": 5,
    },
    "rag": {
        "embedding_model": "all-MiniLM-L6-v2",
        "chunk_size": 150,
        "top_k": 2,
        "fuzzy_match_threshold": 80,
        "entity_corrections": {},
    },
    "tts": {"provider": "kokoro", "default_voice": "af_heart"},
    "stt": {"model": "Qwen/Qwen3-ASR-1.7B", "language": "English"},
    "prompt": {
        "template": (
            "You are a professional AI assistant.\n\n"
            "Conversation History:\n{conversation_history}\n\n"
            "Answer using only the given context in 3-4 lines. "
            "Plain flowing sentences only.\n\n"
            "Context: {context}\n\nQuestion: {query}\n\nAnswer:"
        ),
    },
    "branding": {
        "bot_name": "Assistant",
        "primary_color": "#6aa5ff",
        "welcome_message": "Hi! How can I help you today?",
        "widget_position": "bottom-right",
        "theme": "auto",
    },
    "features": {"backchannels": True, "speaker_verification": True},
}


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("organization_id", "slug", name="uq_workspace_org_slug"),
    )

    organization_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)

    # JSON blob for all editable settings — one row per workspace, easy to migrate.
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=lambda: dict(DEFAULT_SETTINGS))

    organization = relationship("Organization", back_populates="workspaces")
    api_keys = relationship("APIKey", back_populates="workspace", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="workspace", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Workspace {self.slug}>"
