"""
DatabaseConnector protocol + shared data classes.

Rules:
- Connectors only ever perform read operations. The connection URL passed to
  the driver may still be a superuser account (customer's choice), but the
  QueryService enforces SELECT-only at the SQL layer before anything reaches
  the connector's execute().
- Schema discovery reads INFORMATION_SCHEMA (or equivalent). It never reads
  table data.
- Connectors are stateless w.r.t. queries — reuse them from the pool cache,
  but never carry per-request state on the connector object itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class ConnectionSpec:
    """Everything a connector needs to build an engine URL."""
    db_type: str            # "postgres" | "mysql"
    host: str
    port: int
    database: str
    username: str
    password: str
    ssl_enabled: bool = False


@dataclass(frozen=True)
class ConnectionTestResult:
    ok: bool
    message: str            # human-readable — safe to show the user
    version: Optional[str] = None  # e.g. "PostgreSQL 15.4"


@dataclass
class DiscoveredColumn:
    name: str
    data_type: str
    is_nullable: bool
    is_primary_key: bool
    ordinal_position: int


@dataclass
class DiscoveredTable:
    schema_name: str
    table_name: str
    table_type: str        # "table" | "view"
    columns: list[DiscoveredColumn] = field(default_factory=list)


@dataclass
class DiscoveredRelationship:
    constraint_name: str
    from_schema: str
    from_table: str
    from_column: str
    to_schema: str
    to_table: str
    to_column: str


@dataclass
class DiscoveredSchema:
    tables: list[DiscoveredTable] = field(default_factory=list)
    relationships: list[DiscoveredRelationship] = field(default_factory=list)


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]           # tuples in the order of `columns`
    row_count: int
    execution_time_ms: int
    truncated: bool = False     # True if we hit max_rows


@runtime_checkable
class DatabaseConnector(Protocol):
    """
    Every driver (Postgres, MySQL, SQLServer, Oracle, …) implements this.
    Nothing else in the platform is allowed to import the underlying driver.
    """

    name: str  # "postgres" | "mysql" | …

    async def test(self, spec: ConnectionSpec) -> ConnectionTestResult: ...

    async def discover(self, spec: ConnectionSpec) -> DiscoveredSchema: ...

    async def execute_read(
        self,
        spec: ConnectionSpec,
        sql: str,
        params: Optional[dict[str, Any]] = None,
        *,
        max_rows: int = 500,
        timeout_s: int = 15,
    ) -> QueryResult: ...

    async def close_pool(self, spec_key: str) -> None:
        """Best-effort teardown of a cached pool for `spec_key`."""
        ...
