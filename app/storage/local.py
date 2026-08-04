"""Local disk implementation. Files land under STORAGE_ROOT/<path>."""

from __future__ import annotations

import asyncio
import os


class LocalDiskBackend:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def _resolve(self, path: str) -> str:
        # Prevent directory traversal
        target = os.path.normpath(os.path.join(self.root, path))
        if not target.startswith(self.root + os.sep) and target != self.root:
            raise ValueError(f"Path escapes storage root: {path!r}")
        return target

    async def write(self, path: str, data: bytes) -> str:
        target = self._resolve(path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        def _write():
            with open(target, "wb") as f: f.write(data)
        await asyncio.to_thread(_write)
        return path

    async def read(self, path: str) -> bytes:
        target = self._resolve(path)
        def _read():
            with open(target, "rb") as f: return f.read()
        return await asyncio.to_thread(_read)

    async def delete(self, path: str) -> None:
        target = self._resolve(path)
        def _del():
            if os.path.exists(target):
                os.remove(target)
        await asyncio.to_thread(_del)

    async def exists(self, path: str) -> bool:
        return os.path.exists(self._resolve(path))
