"""Token generation, hashing, and lookup for the ICS subscribable feed.

The plaintext token is the user's secret URL component; only the SHA-256
hash is stored in users.ics_token_hash. Lookup is O(log n) via the unique
index. See spec §5.
"""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.user import User


def generate_token() -> tuple[str, str]:
    """Returns (plaintext, hash). Caller stores ONLY the hash.

    Plaintext is ~43 URL-safe chars; hash is 64 lowercase hex chars.
    """
    plaintext = secrets.token_urlsafe(32)
    return plaintext, hash_token(plaintext)


def hash_token(plaintext: str) -> str:
    """SHA-256 hex digest of the plaintext token."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


async def resolve_token(plaintext: str, db: AsyncSession) -> User | None:
    """Return the User whose ics_token_hash matches sha256(plaintext), or None."""
    h = hash_token(plaintext)
    return (await db.execute(select(User).where(User.ics_token_hash == h))).scalar_one_or_none()
