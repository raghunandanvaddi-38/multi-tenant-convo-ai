"""
WorkspaceContext — the request-scoped identity carried through every layer,
replacing the earlier YAML-based TenantContext with a DB-backed workspace.

It's a thin, immutable projection of the Workspace row so downstream services
don't reach into the ORM. Build it via `WorkspaceContext.from_workspace(...)`
at the API boundary (from api_key or dashboard resolvers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.models import Workspace


class _AttrDict(dict):
    """Dict that also exposes keys as attributes (so old code paths that read
    `ctx.config.llm.model` keep working alongside `ctx.settings.llm['model']`)."""
    def __getattr__(self, item):
        try:
            v = self[item]
        except KeyError as e:
            raise AttributeError(item) from e
        return _AttrDict(v) if isinstance(v, dict) else v


@dataclass(frozen=True)
class WorkspaceSettings:
    llm: dict
    rag: dict
    tts: dict
    stt: dict
    prompt_template: str
    branding: dict
    features: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, ws: Workspace) -> "WorkspaceSettings":
        s = ws.settings or {}
        return cls(
            llm=s.get("llm") or {},
            rag=s.get("rag") or {},
            tts=s.get("tts") or {},
            stt=s.get("stt") or {},
            prompt_template=(s.get("prompt") or {}).get("template", ""),
            branding=s.get("branding") or {},
            features=s.get("features") or {},
        )


@dataclass(frozen=True)
class WorkspaceContext:
    workspace_id: str
    organization_id: str
    slug: str
    settings: WorkspaceSettings
    user_id: str = "anonymous"
    conversation_id: str = "default"

    @classmethod
    def from_workspace(
        cls,
        ws: Workspace,
        *,
        user_id: str = "anonymous",
        conversation_id: str = "default",
    ) -> "WorkspaceContext":
        return cls(
            workspace_id=ws.id,
            organization_id=ws.organization_id,
            slug=ws.slug,
            settings=WorkspaceSettings.from_row(ws),
            user_id=user_id,
            conversation_id=conversation_id,
        )

    @property
    def memory_key(self) -> str:
        return f"{self.workspace_id}:{self.conversation_id}"

    # --- Compatibility shims for services written against TenantContext ---
    # ConversationService and AIGateway historically read ctx.tenant_id and
    # ctx.config.<section>.<field>. We expose those as pass-throughs so those
    # services need no changes.

    @property
    def tenant_id(self) -> str:
        return self.workspace_id

    @property
    def config(self):
        s = self.settings
        return _AttrDict({
            "llm": s.llm,
            "rag": s.rag,
            "tts": s.tts,
            "stt": s.stt,
            "branding": s.branding,
            "features": s.features,
            "prompt_template": s.prompt_template,
        })

    def log_fields(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace_id,
            "organization": self.organization_id,
            "user": self.user_id,
            "conversation": self.conversation_id,
        }
