"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from pytest_postgresql import factories
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# pytest-postgresql provides a real ephemeral PG per test session.
# CI provides Postgres via service container; locally it falls back to
# whatever 'pg_ctl' / 'postgres' is on PATH.
_postgresql_proc = factories.postgresql_proc(port=None, unixsocketdir="/tmp")
postgresql = factories.postgresql("_postgresql_proc")


def _async_url_from_psycopg(conn) -> str:  # type: ignore[no-untyped-def]
    p = conn.info
    return f"postgresql+asyncpg://{p.user}:{p.password or ''}@{p.host}:{p.port}/{p.dbname}"


@pytest_asyncio.fixture
async def db_url(postgresql) -> str:  # type: ignore[no-untyped-def]
    return _async_url_from_psycopg(postgresql)


@pytest_asyncio.fixture
async def db_session(db_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(db_url, echo=False, future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    # Create schema for every test session — Phase 1 only has `users`,
    # so we bring it up via metadata.create_all rather than running Alembic
    # in test (Alembic is exercised by its own test in Task 6).
    import trip_tracker.models  # noqa: F401  -- registers all mappers via package __init__
    from trip_tracker.models.base import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        yield session

    await engine.dispose()


@pytest.fixture(autouse=True)
def _set_required_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
    """Populate required env vars so importing config.Settings doesn't fail."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("OIDC_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "trip-tracker")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://trips.example.com/auth/callback")
    monkeypatch.setenv("BASE_URL", "https://trips.example.com")
    monkeypatch.setenv("LOG_FORMAT", "console")
    monkeypatch.setenv("WEBHOOK_SECRET", "x" * 32)
    monkeypatch.setenv("FORWARDEMAIL_RELAY_TOKEN", "x" * 32)
    # Phase 3 — parser pipeline
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    # Phase 4 — search
    monkeypatch.setenv("MEILI_URL", "http://localhost:7700")
    monkeypatch.setenv("MEILI_MASTER_KEY", "x" * 32)
    # Phase 5 — documents (use a writable per-test temp dir instead of /data/documents)
    docs_dir = tmp_path / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DOCUMENTS_DIR", str(docs_dir))


@pytest.fixture(autouse=True)
def _mock_meili_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock the saq Queue used by enqueue_meili_sync to avoid Redis connection."""
    mock_queue = MagicMock()
    mock_queue.enqueue = AsyncMock()
    mock_queue.disconnect = AsyncMock()
    monkeypatch.setattr("trip_tracker.search.sync._build_queue", lambda s: mock_queue)


@pytest.fixture(autouse=True)
def _mock_meili_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock the Meili client built in the app lifespan to avoid HTTP calls.

    The app's lifespan calls build_client(settings) and then
    ensure_indexes_configured(client). Tests that exercise the lifespan
    via app.router.lifespan_context(app) would otherwise fail trying to
    reach a real Meili instance. This fixture replaces build_client with
    a MagicMock that has all the methods ensure_indexes_configured calls.
    """
    fake_index = MagicMock()
    fake_index.update_documents = AsyncMock()
    fake_index.delete_document = AsyncMock()
    fake_index.search = AsyncMock(return_value={"hits": [], "estimatedTotalHits": 0})
    fake_index.update_filterable_attributes = AsyncMock()
    fake_index.update_sortable_attributes = AsyncMock()

    fake_client = MagicMock()
    fake_client.index = MagicMock(return_value=fake_index)
    fake_client.create_index = AsyncMock()
    fake_client.delete_index = AsyncMock()

    # Patch at BOTH the source module AND the app.py import site (app.py uses
    # `from trip_tracker.search.client import build_client`, which captures the
    # function at import time — patching the source module alone misses the call).
    monkeypatch.setattr("trip_tracker.search.client.build_client", lambda s: fake_client)
    monkeypatch.setattr("trip_tracker.app.build_client", lambda s: fake_client)

    # Same for ensure_indexes_configured — its real implementation calls Meili HTTP.
    async def _noop_ensure(meili: object) -> None:
        return None

    monkeypatch.setattr("trip_tracker.app.ensure_indexes_configured", _noop_ensure)


@pytest.fixture(autouse=True)
def _mock_documents_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock the saq Queue used by routes/documents._build_queue to avoid Redis connection."""
    mock_queue = MagicMock()
    mock_queue.enqueue = AsyncMock()
    mock_queue.disconnect = AsyncMock()
    monkeypatch.setattr("trip_tracker.routes.documents._build_queue", lambda s: mock_queue)


@pytest.fixture(autouse=True)
def _mock_worker_doc_queue(monkeypatch: pytest.MonkeyPatch, _set_required_env: None) -> MagicMock:
    """Mock _build_doc_queue in worker.py to avoid Redis connection during tests.

    Depends on _set_required_env so that env vars are set before worker.py is
    imported (worker.py calls Settings() at module level when first imported).

    Yields the mock queue so tests can assert on enqueue calls.
    """
    mock_queue = MagicMock()
    mock_queue.enqueue = AsyncMock()
    mock_queue.disconnect = AsyncMock()
    monkeypatch.setattr("trip_tracker.worker._build_doc_queue", lambda s: mock_queue)
    return mock_queue


@pytest.fixture(autouse=True)
def _mock_map_redis_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock Redis and saq Queue construction in routes/map to avoid real connections.

    The per-trip map handler builds AsyncRedis and Queue per-request.  Tests
    that exercise that handler patch get_cached / _enqueue_weather_refresh at a
    higher level; this fixture just prevents the underlying connection attempts
    from failing.
    """
    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock()
    mock_redis.aclose = AsyncMock()

    mock_queue = MagicMock()
    mock_queue.enqueue = AsyncMock()
    mock_queue.disconnect = AsyncMock()

    monkeypatch.setattr(
        "trip_tracker.routes.map.AsyncRedis",
        MagicMock(from_url=MagicMock(return_value=mock_redis)),
    )
    monkeypatch.setattr(
        "trip_tracker.routes.map.SaqQueue",
        MagicMock(from_url=MagicMock(return_value=mock_queue)),
    )
