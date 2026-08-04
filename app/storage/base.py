"""
StorageBackend protocol — the surface every object store must implement.

Paths are opaque strings shaped like "workspace_id/documents/uuid_original.pdf".
Backends decide how to map paths to a location; callers never assemble absolute
paths themselves.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    async def write(self, path: str, data: bytes) -> str:
        """Store bytes at `path`. Returns the canonical path for retrieval."""
        ...

    async def read(self, path: str) -> bytes: ...

    async def delete(self, path: str) -> None: ...

    async def exists(self, path: str) -> bool: ...
