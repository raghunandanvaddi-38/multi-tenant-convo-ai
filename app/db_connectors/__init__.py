"""
Database connector framework. Business logic depends only on the
DatabaseConnector protocol; the actual driver lives per implementation.
"""

from app.db_connectors.base import (
    DatabaseConnector, ConnectionSpec,
    DiscoveredSchema, DiscoveredTable, DiscoveredColumn, DiscoveredRelationship,
    QueryResult, ConnectionTestResult,
)
from app.db_connectors.registry import get_connector, register_connector
from app.db_connectors.crypto import encrypt_secret, decrypt_secret

__all__ = [
    "DatabaseConnector", "ConnectionSpec",
    "DiscoveredSchema", "DiscoveredTable", "DiscoveredColumn", "DiscoveredRelationship",
    "QueryResult", "ConnectionTestResult",
    "get_connector", "register_connector",
    "encrypt_secret", "decrypt_secret",
]
