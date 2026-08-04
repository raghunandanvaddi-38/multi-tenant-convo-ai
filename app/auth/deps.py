"""
FastAPI dependencies for the two auth paths:

1. `current_user`  — JWT in Authorization header. For dashboard / management.
2. `workspace_from_api_key` — sk_… header. For SDK / widget / server-to-server.

We keep them separate because they authenticate different principals (a human
user vs a workspace-bound service credential).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.security import decode_token, hash_api_key
from app.database import get_session
from app.models import APIKey, Membership, Role, User, Workspace


def _bearer(auth: Optional[str]) -> Optional[str]:
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def current_user(
    authorization: Optional[str] = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> User:
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Wrong token type")

    user_id = payload.get("sub")
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


async def require_org_role(
    organization_id: str,
    minimum: Role,
    user: User,
    session: AsyncSession,
) -> Membership:
    m = (
        await session.execute(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=403, detail="Not a member of this organization")

    order = {Role.member: 0, Role.admin: 1, Role.owner: 2}
    if order[m.role] < order[minimum]:
        raise HTTPException(status_code=403, detail=f"Requires {minimum.value} role")
    return m


class AuthedAPIKey:
    """Resolved API key + its workspace, returned by workspace_from_api_key()."""
    def __init__(self, api_key: APIKey, workspace: Workspace):
        self.api_key = api_key
        self.workspace = workspace


async def workspace_from_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    authorization: Optional[str] = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> AuthedAPIKey:
    """
    Resolve an API key → workspace. Accepts either header form:
      - x-api-key: sk_...
      - Authorization: Bearer sk_...
    Rejects: missing, unknown, revoked, or inactive keys.
    """
    candidate = x_api_key or _bearer(authorization)
    if not candidate or not candidate.startswith("sk_"):
        raise HTTPException(status_code=401, detail="Missing or malformed API key")

    key_hash = hash_api_key(candidate)
    api_key = (
        await session.execute(
            select(APIKey).where(APIKey.key_hash == key_hash).options(
                selectinload(APIKey.workspace)
            )
        )
    ).scalar_one_or_none()

    if api_key is None or not api_key.is_active or api_key.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    api_key.last_used_at = datetime.now(timezone.utc)
    await session.commit()

    return AuthedAPIKey(api_key=api_key, workspace=api_key.workspace)


def require_scope(scope: str):
    """Route dependency factory: rejects if the resolved key lacks the scope."""
    async def _dep(authed: AuthedAPIKey = Depends(workspace_from_api_key)) -> AuthedAPIKey:
        if not authed.api_key.has_scope(scope):
            raise HTTPException(status_code=403, detail=f"API key missing scope: {scope}")
        return authed
    return _dep
