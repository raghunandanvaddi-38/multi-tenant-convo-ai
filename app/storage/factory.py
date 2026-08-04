"""Pick the storage backend based on env. Defaults to local disk under data/uploads."""

from __future__ import annotations

import os
import threading
from typing import Optional

from app.storage.base import StorageBackend
from app.storage.local import LocalDiskBackend


_backend: Optional[StorageBackend] = None
_lock = threading.Lock()


def _default_root() -> str:
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "data", "uploads")


def get_storage() -> StorageBackend:
    global _backend
    if _backend is not None:
        return _backend
    with _lock:
        if _backend is not None:
            return _backend
        kind = os.getenv("STORAGE_BACKEND", "local").lower()
        if kind == "local":
            root = os.getenv("STORAGE_ROOT", _default_root())
            _backend = LocalDiskBackend(root)
        else:
            raise ValueError(f"Unknown STORAGE_BACKEND: {kind}")
        return _backend
