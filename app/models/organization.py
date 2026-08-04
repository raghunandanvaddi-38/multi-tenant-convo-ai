from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.engine import Base


class Organization(Base):
    __tablename__ = "organizations"

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)

    workspaces = relationship("Workspace", back_populates="organization", cascade="all, delete-orphan")
    memberships = relationship("Membership", back_populates="organization", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Organization {self.slug}>"
