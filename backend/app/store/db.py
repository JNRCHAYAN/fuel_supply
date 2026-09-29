"""Engine and session lifecycle for the storage layer.

Owned by A3.  See CONTRACT.md section 6 ("Storage") and section 2 (SQLAlchemy 2.0
+ SQLite via ``aiosqlite``, swappable to Postgres by URL).

Nothing in this module reaches out to the simulator, the network or the LLM.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

logger = logging.getLogger(__name__)

__all__ = ["Base", "Database", "DEFAULT_DATABASE_URL", "resolve_database_url", "utc_now"]

#: Documented default from CONTRACT.md section 4.  Only used when A1's
#: ``app.config.get_settings()`` is unavailable; every real caller passes a URL.
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./data/fuel.db"


def utc_now() -> float:
    """Wall-clock timestamp used for audit columns.

    Deliberately ``time.time()`` and not ``time.monotonic()``: these columns are
    read back after a restart, and monotonic values are meaningless across
    processes.
    """
    return time.time()


class Base(DeclarativeBase):
    """Declarative base shared by every table in ``app.store.models``."""


def resolve_database_url(explicit: str | None = None) -> str:
    """Return the database URL to use, honouring ``DATABASE_URL`` from settings.

    A1 owns ``app.config``.  It is imported lazily so the storage layer can be
    constructed (and tested) while that module is still being written; the
    fallback is the default documented in CONTRACT.md section 4, never an
    invented value.
    """
    if explicit:
        return explicit
    try:
        from app.config import get_settings  # noqa: PLC0415 - deferred on purpose
    except ImportError as exc:  # pragma: no cover - depends on peer timing
        logger.warning(
            "app.config (A1) is not importable (%s); falling back to the documented "
            "default DATABASE_URL %r",
            exc,
            DEFAULT_DATABASE_URL,
        )
        return DEFAULT_DATABASE_URL
    return get_settings().database_url


def _is_memory_url(url: str) -> bool:
    return ":memory:" in url or "mode=memory" in url


def _ensure_sqlite_directory(url: str) -> None:
    """Create the parent directory of a file-backed SQLite database.

    SQLite creates the *file* but never the directory above it: against a fresh
    checkout, where ``backend/data/`` does not yet exist, the first connection
    raises ``OperationalError: unable to open database file``.  Upstream catches
    that and reports a degraded dependency, so the symptom is not a crash -- it
    is a service that answers every request and persists nothing.

    Best effort by design.  A read-only or otherwise unwritable directory must
    still surface through ``ping()``/the first real query, with the original
    error, rather than as an exception raised from a constructor.
    """
    if _is_memory_url(url):
        return
    try:
        _, _, path = url.partition(":///")
        # Drop SQLite's URI query suffix ("?check_same_thread=false"), if any.
        path = path.split("?", 1)[0]
        if not path:
            return
        parent = Path(path).expanduser().parent
        if str(parent) not in ("", ".") and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - unwritable directory, reported by ping()
        logger.debug("could not create the SQLite parent directory for %r", url, exc_info=True)



def _engine_options(url: str, *, echo: bool) -> dict[str, object]:
    """Engine kwargs, including the one real SQLite trap.

    Without ``StaticPool`` every connection to ``sqlite+aiosqlite:///:memory:``
    gets its own private database, so ``create_all`` and the first query would
    run against different databases.  File URLs keep SQLAlchemy's default pool.
    """
    if _is_memory_url(url):
        return {
            "echo": echo,
            "poolclass": StaticPool,
            "connect_args": {"check_same_thread": False},
        }
    return {"echo": echo}


class Database:
    """Owns the async engine and the session factory.

    ``Repository`` composes one of these; the API layer may also use it directly
    for the ``database`` component of ``/api/v1/status``.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        echo: bool = False,
        engine: AsyncEngine | None = None,
    ) -> None:
        self.url = resolve_database_url(url)
        _ensure_sqlite_directory(self.url)
        self.engine: AsyncEngine = (
            engine if engine is not None else create_async_engine(self.url, **_engine_options(self.url, echo=echo))
        )
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        self._initialized = False

    async def init(self) -> None:
        """Create every table.  Idempotent: safe to call more than once."""
        # Import for the side effect of registering the tables on ``Base.metadata``.
        import app.store.models  # noqa: F401,PLC0415

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self._initialized = True

    @property
    def initialized(self) -> bool:
        return self._initialized

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session that commits on success and rolls back on failure."""
        async with self.session_factory() as session:
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

    async def dispose(self) -> None:
        await self.engine.dispose()
        self._initialized = False

    async def ping(self) -> bool:
        """Cheap liveness probe used by the API status endpoint."""
        from sqlalchemy import text  # noqa: PLC0415

        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - a health probe must never raise
            logger.warning("database ping failed", exc_info=True)
            return False
        return True
