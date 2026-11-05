"""Database engine and session helpers (SQLAlchemy 2.0, SQLite by default)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    # WAL lets the API read while background jobs write.
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        url = get_settings().db_url
        kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite") else {}
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _configure_sqlite)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    engine = get_engine()
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)


def _add_missing_columns(engine: Engine) -> None:
    """Add columns that newer versions introduced to tables an older version created.

    There is no migration tool; new tables come from ``create_all`` and new
    columns (always nullable) from here.
    """
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(dialect=engine.dialect)}"
                for fk in col.foreign_keys:
                    ondelete = f" ON DELETE {fk.ondelete}" if fk.ondelete else ""
                    ddl += f" REFERENCES {fk.column.table.name}({fk.column.name}){ondelete}"
                conn.execute(text(ddl))


def reset_engine() -> None:
    """Forget the current engine so the next call picks up new settings."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session
