"""Document storage Protocol + LocalFsStorage. Spec §5."""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

_KEY_RE = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{64}$")
_CHUNK = 64 * 1024


def _validate_key(key: str) -> None:
    if not _KEY_RE.fullmatch(key):
        raise ValueError(f"invalid storage_key: {key!r}")


class StorageBackend(Protocol):
    """File storage abstraction. v0.5.0 ships LocalFsStorage; S3 in Phase 5.x.

    NOTE: `open` is `async def` (returning AsyncIterator[bytes]) so future S3
    backends can do an async metadata check before yielding. Call sites use
    `async for chunk in await storage.open(key):` (double-await — the await
    resolves the coroutine, the async-for iterates).
    """

    async def put(self, sha256: str, content: bytes) -> str:
        """Persist content. Returns storage_key. Idempotent on identical content."""

    async def open(self, storage_key: str) -> AsyncIterator[bytes]:
        """Iterate file content in chunks. Raises ValueError on bad key."""

    async def delete(self, storage_key: str) -> None:
        """Remove file. Idempotent: missing file is not an error.

        Raises ValueError on a malformed key (path-traversal guard).
        """

    def absolute_path(self, storage_key: str) -> str | None:
        """Return absolute FS path for X-Accel mode, or None if not local."""


class LocalFsStorage:
    """Filesystem-backed StorageBackend. Content-addressed under <root>/<sha[:2]>/<sha>."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    async def put(self, sha256: str, content: bytes) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError(f"invalid sha256: {sha256!r}")
        key = f"{sha256[:2]}/{sha256}"
        target = self._root / key
        if target.exists():
            return key
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, target)
        return key

    async def open(self, storage_key: str) -> AsyncIterator[bytes]:
        _validate_key(storage_key)
        path = self._root / storage_key

        async def _iter() -> AsyncIterator[bytes]:
            with path.open("rb") as fh:
                while chunk := fh.read(_CHUNK):
                    yield chunk

        return _iter()

    async def delete(self, storage_key: str) -> None:
        _validate_key(storage_key)
        path = self._root / storage_key
        try:
            path.unlink()
        except FileNotFoundError:
            return  # idempotent

    def absolute_path(self, storage_key: str) -> str:
        _validate_key(storage_key)
        return str(self._root / storage_key)
