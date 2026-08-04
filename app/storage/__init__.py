"""Storage backend for uploaded documents. Swap implementations via config."""

from app.storage.base import StorageBackend
from app.storage.local import LocalDiskBackend
from app.storage.factory import get_storage

__all__ = ["StorageBackend", "LocalDiskBackend", "get_storage"]
