"""SQLAlchemy engine + session dependency (Postgres via psycopg 3)."""
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine():
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,      # survive the pooler dropping idle connections
        pool_size=5,
        max_overflow=5,
        # Supabase's transaction-mode pooler (port 6543) does not support
        # prepared statements; disabling them is harmless on session mode too.
        connect_args={"prepare_threshold": None},
    )


@lru_cache
def _session_factory():
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)


def get_db():
    """FastAPI dependency: one session per request, always closed."""
    db = _session_factory()()
    try:
        yield db
    finally:
        db.close()
