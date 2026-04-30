"""`python -m trip_tracker` → uvicorn server (default) or admin subcommand."""

from __future__ import annotations

import asyncio
import sys

import uvicorn


def main() -> None:
    uvicorn.run(
        "trip_tracker.app:create_app",
        host="0.0.0.0",  # noqa: S104  # nosec B104 - bound inside container; reverse proxy enforces auth
        port=8000,
        factory=True,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_config=None,  # we own logging
    )


async def _parse_pending(*, max_emails: int = 1000, dry_run: bool = False) -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from trip_tracker.config import Settings
    from trip_tracker.ingest.webhook import enqueue_parse
    from trip_tracker.models.raw_email import RawEmail

    settings = Settings()
    engine = create_async_engine(str(settings.database_url))
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with SessionMaker() as db:
        rows = (
            (
                await db.execute(
                    select(RawEmail.id).where(RawEmail.parse_status == "pending").limit(max_emails)
                )
            )
            .scalars()
            .all()
        )
        print(f"Found {len(rows)} pending RawEmails")
        if dry_run:
            await engine.dispose()
            return
        for rid in rows:
            await enqueue_parse(settings, rid)
        print(f"Enqueued {len(rows)} parse jobs")
    await engine.dispose()


async def _reindex(*, batch_size: int = 100, dry_run: bool = False) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from trip_tracker.config import Settings
    from trip_tracker.search.client import build_client
    from trip_tracker.search.reindex import reindex_all

    settings = Settings()
    engine = create_async_engine(str(settings.database_url))
    meili = build_client(settings)
    counts = await reindex_all(engine, meili, batch_size=batch_size, dry_run=dry_run)
    await engine.dispose()
    print(
        f"Reindex complete: trips={counts['trips']} "
        f"segments={counts['segments']} documents={counts['documents']}"
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "parse_pending":
        dry_run = "--dry-run" in sys.argv
        max_emails = 1000
        for arg in sys.argv[2:]:
            if arg.startswith("--max-emails="):
                max_emails = int(arg.split("=", 1)[1])
        asyncio.run(_parse_pending(max_emails=max_emails, dry_run=dry_run))
    elif len(sys.argv) > 1 and sys.argv[1] == "reindex":
        dry_run = "--dry-run" in sys.argv
        batch_size = 100
        for arg in sys.argv[2:]:
            if arg.startswith("--batch-size="):
                batch_size = int(arg.split("=", 1)[1])
        asyncio.run(_reindex(batch_size=batch_size, dry_run=dry_run))
    else:
        main()
