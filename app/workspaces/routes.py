"""Management REST routes — authenticated with a user JWT."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_user
from app.database import get_session
from app.models import Organization, User, Workspace
from app.workspaces.service import ManagementService


router = APIRouter(prefix="/v1", tags=["management"])


def _org_out(o: Organization) -> dict:
    return {"id": o.id, "slug": o.slug, "name": o.name, "created_at": o.created_at.isoformat()}


def _ws_out(w: Workspace) -> dict:
    return {
        "id": w.id,
        "organization_id": w.organization_id,
        "slug": w.slug,
        "name": w.name,
        "settings": w.settings,
        "created_at": w.created_at.isoformat(),
    }


def _key_out(k) -> dict:
    return {
        "id": k.id,
        "name": k.name,
        "prefix": k.prefix,
        "scopes": k.scope_list(),
        "is_active": k.is_active,
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
        "created_at": k.created_at.isoformat(),
    }


class CreateOrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class CreateWorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    slug: str = ""


class UpdateWorkspaceIn(BaseModel):
    settings: dict


class CreateAPIKeyIn(BaseModel):
    name: str = ""
    scopes: str = "chat,read"


class RevertPromptIn(BaseModel):
    version: int


# --- Orgs -------------------------------------------------------------

@router.get("/organizations")
async def list_orgs(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    orgs = await ManagementService(session).list_orgs_for_user(user)
    return [_org_out(o) for o in orgs]


@router.post("/organizations")
async def create_org(body: CreateOrgIn, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    org = await ManagementService(session).create_org(user, body.name)
    return _org_out(org)


# --- Workspaces -------------------------------------------------------

@router.get("/organizations/{org_id}/workspaces")
async def list_workspaces(
    org_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    rows = await ManagementService(session).list_workspaces(user, org_id)
    return [_ws_out(w) for w in rows]


@router.post("/organizations/{org_id}/workspaces")
async def create_workspace(
    org_id: str, body: CreateWorkspaceIn,
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session),
):
    ws = await ManagementService(session).create_workspace(user, org_id, body.name, body.slug)
    return _ws_out(ws)


@router.get("/workspaces/{workspace_id}")
async def get_workspace(
    workspace_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    ws = await ManagementService(session).get_workspace(user, workspace_id)
    return _ws_out(ws)


@router.patch("/workspaces/{workspace_id}")
async def update_workspace(
    workspace_id: str, body: UpdateWorkspaceIn,
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session),
):
    ws = await ManagementService(session).update_workspace_settings(user, workspace_id, body.settings)
    return _ws_out(ws)


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    await ManagementService(session).delete_workspace(user, workspace_id)
    return {"ok": True}


# --- Prompts ----------------------------------------------------------

@router.get("/workspaces/{workspace_id}/prompt-versions")
async def list_prompt_versions(
    workspace_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    rows = await ManagementService(session).list_prompt_versions(user, workspace_id)
    return [
        {
            "id": v.id, "version": v.version, "template": v.template,
            "author_user_id": v.author_user_id, "note": v.note,
            "created_at": v.created_at.isoformat(),
        }
        for v in rows
    ]


@router.post("/workspaces/{workspace_id}/prompt-versions/revert")
async def revert_prompt(
    workspace_id: str, body: RevertPromptIn,
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session),
):
    ws = await ManagementService(session).revert_prompt(user, workspace_id, body.version)
    return _ws_out(ws)


# --- API keys ---------------------------------------------------------

@router.get("/workspaces/{workspace_id}/api-keys")
async def list_api_keys(
    workspace_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    rows = await ManagementService(session).list_api_keys(user, workspace_id)
    return [_key_out(k) for k in rows]


@router.post("/workspaces/{workspace_id}/api-keys")
async def create_api_key(
    workspace_id: str, body: CreateAPIKeyIn,
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session),
):
    k, plaintext = await ManagementService(session).create_api_key(
        user, workspace_id, name=body.name, scopes=body.scopes
    )
    out = _key_out(k)
    out["key"] = plaintext  # shown once
    return out


@router.delete("/workspaces/{workspace_id}/api-keys/{key_id}")
async def revoke_api_key(
    workspace_id: str, key_id: str,
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session),
):
    await ManagementService(session).revoke_api_key(user, workspace_id, key_id)
    return {"ok": True}
