"""Engine construction and schema bootstrap.

Supports SQLite (the zero-config default) and PostgreSQL from the same
``DATABASE_URL``. SQLite gets a few pragmas that matter a great deal when
loading a few hundred thousand rows at a time.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import Connection

from ..config import Settings
from .models import metadata

log = logging.getLogger(__name__)


def _prepare_sqlite_path(url: str) -> None:
    """Make sure the directory holding a SQLite file exists."""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return
    raw = url[len(prefix) :]
    if not raw or raw == ":memory:":
        return
    path = Path(raw)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        log.info("created database directory %s", path.parent)


def create_db_engine(settings: Settings, *, echo: bool = False) -> Engine:
    """Build the SQLAlchemy engine described by ``DATABASE_URL``."""
    url = settings.database_url
    is_sqlite = url.startswith("sqlite")
    _prepare_sqlite_path(url)

    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if is_sqlite:
        # A single writer plus many readers is exactly this service's shape.
        kwargs["connect_args"] = {"timeout": 30, "check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 10

    engine = create_engine(url, **kwargs)

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            # WAL lets the API keep serving reads while a sync writes.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA temp_store=MEMORY")
            cur.execute("PRAGMA cache_size=-64000")  # ~64 MB page cache
            cur.close()

    return engine


def init_schema(engine: Engine) -> None:
    """Create any missing tables and indexes. Safe to call repeatedly."""
    metadata.create_all(engine, checkfirst=True)
    log.debug("schema ensured on %s", engine.url.render_as_string(hide_password=True))


def optimize(conn: Connection) -> None:
    """Post-load housekeeping so the planner has fresh statistics."""
    dialect = conn.engine.dialect.name
    if dialect == "sqlite" or dialect == "postgresql":
        conn.exec_driver_sql("ANALYZE")
