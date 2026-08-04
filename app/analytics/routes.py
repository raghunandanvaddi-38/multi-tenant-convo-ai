"""Analytics routes — accessible via user JWT (dashboard) or read-scope API key."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.service import summary
from app.auth.deps import AuthedAPIKey, current_user, require_scope
from app.database import get_session
from app.models import User, Workspace
from app.workspaces.service import ManagementService


router = APIRouter(prefix="/v1/analytics", tags=["analytics"])


@router.get("/summary")
async def summary_by_apikey(
    days: int = Query(default=7, ge=1, le=90),
    authed: AuthedAPIKey = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
):
    """Analytics for the API key's own workspace."""
    return await summary(session, authed.workspace.id, days=days)


@router.get("/workspaces/{workspace_id}/summary")
async def summary_by_user(
    workspace_id: str,
    days: int = Query(default=7, ge=1, le=90),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Dashboard-facing analytics — verifies org membership."""
    await ManagementService(session).get_workspace(user, workspace_id)
    return await summary(session, workspace_id, days=days)
