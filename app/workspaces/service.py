"""
Org / Workspace / APIKey / Prompt management service.

Called by the management routes AND by the dashboard. Every mutation checks
membership + role via `require_org_role`. Keeps route handlers thin.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_org_role
from app.auth.security import generate_api_key
from app.models import APIKey, Membership, Organization, PromptVersion, Role, User, Workspace
from app.models.workspace import DEFAULT_SETTINGS


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return s or "item"


def _deep_merge(base: dict, update: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (update or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class ManagementService:
    def __init__(self, session: AsyncSession):
        self.session = session

    # --- Orgs -----------------------------------------------------------
    async def list_orgs_for_user(self, user: User) -> list[Organization]:
        rows = (
            await self.session.execute(
                select(Organization)
                .join(Membership, Membership.organization_id == Organization.id)
                .where(Membership.user_id == user.id)
                .order_by(Organization.created_at)
            )
        ).scalars().all()
        return list(rows)

    async def create_org(self, user: User, name: str) -> Organization:
        slug_base = _slugify(name)
        # Ensure unique slug
        n = 0
        while True:
            candidate = slug_base if n == 0 else f"{slug_base}-{n}"
            exists = (await self.session.execute(select(Organization).where(Organization.slug == candidate))).scalar_one_or_none()
            if exists is None:
                break
            n += 1
        org = Organization(slug=candidate, name=name)
        self.session.add(org)
        await self.session.flush()
        self.session.add(Membership(user_id=user.id, organization_id=org.id, role=Role.owner))
        await self.session.commit()
        return org

    # --- Workspaces -----------------------------------------------------
    async def list_workspaces(self, user: User, org_id: str) -> list[Workspace]:
        await require_org_role(org_id, Role.member, user, self.session)
        rows = (
            await self.session.execute(
                select(Workspace).where(Workspace.organization_id == org_id).order_by(Workspace.created_at)
            )
        ).scalars().all()
        return list(rows)

    async def create_workspace(self, user: User, org_id: str, name: str, slug: Optional[str] = None) -> Workspace:
        await require_org_role(org_id, Role.admin, user, self.session)
        base = _slugify(slug or name)
        n = 0
        while True:
            candidate = base if n == 0 else f"{base}-{n}"
            exists = (
                await self.session.execute(
                    select(Workspace).where(Workspace.organization_id == org_id, Workspace.slug == candidate)
                )
            ).scalar_one_or_none()
            if exists is None:
                break
            n += 1
        ws = Workspace(organization_id=org_id, slug=candidate, name=name)
        self.session.add(ws)
        await self.session.commit()
        return ws

    async def get_workspace(self, user: User, workspace_id: str, minimum: Role = Role.member) -> Workspace:
        ws = await self.session.get(Workspace, workspace_id)
        if ws is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        await require_org_role(ws.organization_id, minimum, user, self.session)
        return ws

    async def update_workspace_settings(
        self, user: User, workspace_id: str, patch: dict[str, Any]
    ) -> Workspace:
        ws = await self.get_workspace(user, workspace_id, minimum=Role.admin)
        merged = _deep_merge(ws.settings or DEFAULT_SETTINGS, patch)
        # If prompt.template changed, snapshot old version
        old_tpl = ((ws.settings or {}).get("prompt") or {}).get("template", "")
        new_tpl = ((patch.get("prompt") or {}).get("template"))
        if new_tpl is not None and new_tpl != old_tpl:
            latest = (
                await self.session.execute(
                    select(func.max(PromptVersion.version)).where(PromptVersion.workspace_id == ws.id)
                )
            ).scalar()
            next_v = (latest or 0) + 1
            self.session.add(PromptVersion(
                workspace_id=ws.id, version=next_v,
                template=old_tpl or new_tpl,  # snapshot the OLD, ignore if empty
                author_user_id=user.id,
                note=(patch.get("prompt") or {}).get("note", ""),
            ))
        ws.settings = merged
        await self.session.commit()
        return ws

    async def delete_workspace(self, user: User, workspace_id: str) -> None:
        ws = await self.get_workspace(user, workspace_id, minimum=Role.admin)
        await self.session.delete(ws)
        await self.session.commit()

    # --- Prompts --------------------------------------------------------
    async def list_prompt_versions(self, user: User, workspace_id: str) -> list[PromptVersion]:
        ws = await self.get_workspace(user, workspace_id)
        rows = (
            await self.session.execute(
                select(PromptVersion)
                .where(PromptVersion.workspace_id == ws.id)
                .order_by(PromptVersion.version.desc())
            )
        ).scalars().all()
        return list(rows)

    async def revert_prompt(self, user: User, workspace_id: str, version: int) -> Workspace:
        ws = await self.get_workspace(user, workspace_id, minimum=Role.admin)
        pv = (
            await self.session.execute(
                select(PromptVersion).where(
                    PromptVersion.workspace_id == ws.id, PromptVersion.version == version
                )
            )
        ).scalar_one_or_none()
        if pv is None:
            raise HTTPException(status_code=404, detail="Prompt version not found")
        return await self.update_workspace_settings(user, workspace_id, {"prompt": {"template": pv.template}})

    # --- API keys -------------------------------------------------------
    async def list_api_keys(self, user: User, workspace_id: str) -> list[APIKey]:
        ws = await self.get_workspace(user, workspace_id)
        rows = (
            await self.session.execute(
                select(APIKey).where(APIKey.workspace_id == ws.id).order_by(APIKey.created_at.desc())
            )
        ).scalars().all()
        return list(rows)

    async def create_api_key(
        self, user: User, workspace_id: str, *, name: str, scopes: str = "chat,read"
    ) -> tuple[APIKey, str]:
        ws = await self.get_workspace(user, workspace_id, minimum=Role.admin)
        # sanitize scopes
        allowed = {"chat", "read", "admin"}
        parsed = ",".join(s.strip() for s in scopes.split(",") if s.strip() in allowed) or "chat,read"
        plaintext, prefix, khash = generate_api_key()
        k = APIKey(workspace_id=ws.id, name=name or "unnamed", prefix=prefix, key_hash=khash, scopes=parsed)
        self.session.add(k)
        await self.session.commit()
        return k, plaintext

    async def revoke_api_key(self, user: User, workspace_id: str, key_id: str) -> None:
        ws = await self.get_workspace(user, workspace_id, minimum=Role.admin)
        k = await self.session.get(APIKey, key_id)
        if k is None or k.workspace_id != ws.id:
            raise HTTPException(status_code=404, detail="API key not found")
        from datetime import datetime, timezone
        k.is_active = False
        k.revoked_at = datetime.now(timezone.utc)
        await self.session.commit()
