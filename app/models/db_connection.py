from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.engine import Base


class DBType(str, enum.Enum):
    postgres = "postgres"
    mysql = "mysql"
    # Phase 2+:
    # sqlserver = "sqlserver"
    # oracle = "oracle"


class ConnectionStatus(str, enum.Enum):
    pending = "pending"      # created, not yet tested
    active = "active"        # last test succeeded, schema discovered
    error = "error"          # last test or discovery failed


class DBConnection(Base):
    """
    A workspace's registered relational database. Credentials are stored
    encrypted (Fernet, key derived from SECRET_KEY); never returned in API
    responses. Removing a workspace cascades and removes its connections,
    tables, columns, relationships, and query logs.
    """
    __tablename__ = "db_connections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_dbc_ws_name"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    db_type: Mapped[DBType] = mapped_column(Enum(DBType), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    database_name: Mapped[str] = mapped_column(String(120), nullable=False)
    username: Mapped[str] = mapped_column(String(120), nullable=False)
    encrypted_password: Mapped[str] = mapped_column(Text, nullable=False)
    ssl_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    status: Mapped[ConnectionStatus] = mapped_column(
        Enum(ConnectionStatus), default=ConnectionStatus.pending, nullable=False, index=True
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_tested_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_refreshed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    tables = relationship("DBTable", back_populates="connection", cascade="all, delete-orphan")
    relationships = relationship("DBRelationship", back_populates="connection", cascade="all, delete-orphan")


class DBTable(Base):
    __tablename__ = "db_tables"
    __table_args__ = (
        UniqueConstraint("connection_id", "schema_name", "table_name", name="uq_dbt_conn_schema_name"),
    )

    connection_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("db_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    schema_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    table_name: Mapped[str] = mapped_column(String(160), nullable=False)
    table_type: Mapped[str] = mapped_column(String(20), default="table", nullable=False)  # 'table' | 'view'
    allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    row_count_hint: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    connection = relationship("DBConnection", back_populates="tables")
    columns = relationship("DBColumn", back_populates="table", cascade="all, delete-orphan")

    @property
    def qualified_name(self) -> str:
        return f"{self.schema_name}.{self.table_name}" if self.schema_name else self.table_name


class DBColumn(Base):
    __tablename__ = "db_columns"
    __table_args__ = (
        UniqueConstraint("table_id", "column_name", name="uq_dbc_table_col"),
    )

    table_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("db_tables.id", ondelete="CASCADE"), nullable=False, index=True
    )
    column_name: Mapped[str] = mapped_column(String(160), nullable=False)
    data_type: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    is_nullable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_primary_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ordinal_position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    table = relationship("DBTable", back_populates="columns")


class DBRelationship(Base):
    __tablename__ = "db_relationships"

    connection_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("db_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    constraint_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    from_table_id: Mapped[str] = mapped_column(String(32), ForeignKey("db_tables.id", ondelete="CASCADE"))
    from_column: Mapped[str] = mapped_column(String(160), nullable=False)
    to_table_id: Mapped[str] = mapped_column(String(32), ForeignKey("db_tables.id", ondelete="CASCADE"))
    to_column: Mapped[str] = mapped_column(String(160), nullable=False)

    connection = relationship("DBConnection", back_populates="relationships")


class DBQueryLog(Base):
    """
    Audit trail. Every executed query gets a row here — success or failure.
    Passwords and credentials are NEVER written into this table; the sql_text
    column stores the query as executed (which cannot contain credentials).
    """
    __tablename__ = "db_query_logs"

    connection_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("db_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workspace_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sql_text: Mapped[str] = mapped_column(Text, nullable=False)
    execution_time_ms: Mapped[int] = mapped_column(Integer, default=0)
    rows_returned: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    user_context: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)  # e.g. NL question
