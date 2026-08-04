"""Auth routes: signup, login, refresh, me."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_user
from app.auth.service import AuthService
from app.database import get_session
from app.models import User


router = APIRouter(prefix="/auth", tags=["auth"])


class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    display_name: str = ""


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


@router.post("/signup")
async def signup(body: SignupIn, session: AsyncSession = Depends(get_session)):
    _, tokens = await AuthService(session).signup(
        email=body.email, password=body.password, display_name=body.display_name
    )
    return tokens


@router.post("/login")
async def login(body: LoginIn, session: AsyncSession = Depends(get_session)):
    _, tokens = await AuthService(session).login(email=body.email, password=body.password)
    return tokens


@router.post("/refresh")
async def refresh(body: RefreshIn, session: AsyncSession = Depends(get_session)):
    return await AuthService(session).refresh(refresh_token=body.refresh_token)


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "is_superuser": user.is_superuser,
    }
