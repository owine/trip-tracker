"""LocalFsStorage round-trips, path-traversal guard, idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest

from trip_tracker.documents.storage import LocalFsStorage, StorageBackend

GOOD_KEY = "ab/" + "a" * 64
BAD_KEYS = [
    "../etc/passwd",
    "ab/../../../tmp/x",
    "AB/" + "a" * 64,  # non-lower hex prefix
    "ab/" + "a" * 63,  # short
    "ab/" + "a" * 65,  # long
    "ab/" + "g" * 64,  # non-hex
    "abc/" + "a" * 64,  # 3-char prefix
    "/absolute",
]


@pytest.fixture
def storage(tmp_path: Path) -> LocalFsStorage:
    return LocalFsStorage(tmp_path)


@pytest.mark.asyncio
async def test_put_then_open_round_trips(storage: LocalFsStorage) -> None:
    sha = "a" * 64
    key = await storage.put(sha, b"hello world")
    assert key == f"{sha[:2]}/{sha}"
    chunks = []
    async for chunk in await storage.open(key):
        chunks.append(chunk)
    assert b"".join(chunks) == b"hello world"


@pytest.mark.asyncio
async def test_put_is_idempotent_for_existing_key(storage: LocalFsStorage) -> None:
    sha = "b" * 64
    k1 = await storage.put(sha, b"v1")
    k2 = await storage.put(sha, b"v1")
    assert k1 == k2
    chunks = []
    async for chunk in await storage.open(k1):
        chunks.append(chunk)
    assert b"".join(chunks) == b"v1"


@pytest.mark.asyncio
async def test_delete_is_idempotent(storage: LocalFsStorage) -> None:
    sha = "c" * 64
    key = await storage.put(sha, b"x")
    await storage.delete(key)
    await storage.delete(key)  # missing — must not raise


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", BAD_KEYS)
async def test_open_rejects_path_traversal(storage: LocalFsStorage, bad: str) -> None:
    with pytest.raises(ValueError):
        await storage.open(bad)  # ValueError raises during the await, before iteration


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", BAD_KEYS)
async def test_delete_rejects_path_traversal(storage: LocalFsStorage, bad: str) -> None:
    with pytest.raises(ValueError):
        await storage.delete(bad)


@pytest.mark.parametrize("bad", BAD_KEYS)
def test_absolute_path_rejects_path_traversal(storage: LocalFsStorage, bad: str) -> None:
    with pytest.raises(ValueError):
        storage.absolute_path(bad)


def test_absolute_path_returns_full_path_for_good_key(
    storage: LocalFsStorage, tmp_path: Path
) -> None:
    assert storage.absolute_path(GOOD_KEY) == str(tmp_path / GOOD_KEY)


def test_protocol_satisfied(storage: LocalFsStorage) -> None:
    """Static check: LocalFsStorage matches StorageBackend Protocol."""
    sb: StorageBackend = storage  # would mypy-fail if shape mismatches
    assert sb is storage
