"""
Registry: db_type string → DatabaseConnector instance.

Import side-effects register the built-in connectors. To add a new one:
  1. Implement DatabaseConnector in app/db_connectors/<name>_connector.py
  2. Call register_connector("<db_type>", <instance>) at import time
  3. Add "<db_type>" to app.models.db_connection.DBType enum
"""

from __future__ import annotations

import threading
from typing import Optional

from app.db_connectors.base import DatabaseConnector


_registry: dict[str, DatabaseConnector] = {}
_lock = threading.Lock()


def register_connector(name: str, connector: DatabaseConnector) -> None:
    with _lock:
        _registry[name] = connector


def get_connector(db_type: str) -> DatabaseConnector:
    connector = _registry.get(db_type)
    if connector is None:
        raise ValueError(f"No connector registered for db_type={db_type!r}")
    return connector


def known_types() -> list[str]:
    return sorted(_registry.keys())


# Import the built-in connectors so they self-register.
# We import inside a function to avoid a top-level import cycle.
def _bootstrap() -> None:
    from app.db_connectors.postgres_connector import PostgresConnector
    from app.db_connectors.mysql_connector import MySQLConnector
    register_connector("postgres", PostgresConnector())
    register_connector("mysql", MySQLConnector())


_bootstrap()
