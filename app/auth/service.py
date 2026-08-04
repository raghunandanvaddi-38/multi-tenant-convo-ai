"""
Signup / login / refresh business logic.

Signup creates a User + a personal Organization + Owner Membership + one
default Workspace. This gets the user to a usable state in one call — no
"now go create an org" second step.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import (
    create_access_token, create_refresh_token, decode_token,
    hash_password, verify_password,
)
from app.models import Membership, Organization, Role, User, Workspace


log = logging.getLogger("auth.service")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return s or "workspace"


async def _unique_slug(session: AsyncSession, table, column, base: str) -> str:
    slug = _slugify(base)
    candidate = slug
    n = 1
    while True:
        exists = (
            await session.execute(select(table).where(column == candidate).limit(1))
        ).scalar_one_or_none()
        if exists is None:
            return candidate
        n += 1
        candidate = f"{slug}-{n}"


class AuthService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def signup(self, email: str, password: str, display_name: str = "") -> tuple[User, dict]:
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            raise HTTPException(status_code=400, detail="Invalid email")
        if not password or len(password) < 8:
            raise HTTPException(status_code=400, detail="Password must be ≥8 chars")

        exists = (
            await self.session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if exists is not None:
            raise HTTPException(status_code=409, detail="Email already registered")

        user = User(email=email, password_hash=hash_password(password), display_name=display_name or email.split("@")[0])
        self.session.add(user)
        await self.session.flush()

        # Personal org + default workspace
        org_slug = await _unique_slug(self.session, Organization, Organization.slug, display_name or email.split("@")[0])
        org = Organization(slug=org_slug, name=(display_name or email.split("@")[0]).title())
        self.session.add(org)
        await self.session.flush()

        self.session.add(Membership(user_id=user.id, organization_id=org.id, role=Role.owner))

        ws = Workspace(organization_id=org.id, slug="default", name="Default assistant")
        self.session.add(ws)

        await self.session.commit()
        log.info(f"[auth] signup user={email} org={org.slug}")
        return user, self._tokens(user)

    async def login(self, email: str, password: str) -> tuple[User, dict]:
        email = (email or "").strip().lower()
        user = (
            await self.session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if user is None or not verify_password(password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account disabled")
        return user, self._tokens(user)

    async def refresh(self, refresh_token: str) -> dict:
        try:
            payload = decode_token(refresh_token)
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Wrong token type")
        user = await self.session.get(User, payload.get("sub"))
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="User not found")
        return self._tokens(user)

    @staticmethod
    def _tokens(user: User) -> dict:
        return {
            "access_token": create_access_token(user.id),
            "refresh_token": create_refresh_token(user.id),
            "token_type": "bearer",
        }
