"""Engine/session plumbing plus schema bootstrap."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(settings.database_url, pool_pre_ping=True, future=True)


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on exception."""
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def apply_migrations(engine: Engine | None = None) -> list[str]:
    """Apply every .sql file in migrations/ in name order. Files are idempotent."""
    engine = engine or get_engine()
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text()
        with engine.begin() as conn:
            # exec_driver_sql bypasses SQLAlchemy's ":name" bind parsing, which
            # would otherwise choke on Postgres "::type" casts.
            conn.exec_driver_sql(sql)
        applied.append(path.name)
    return applied
