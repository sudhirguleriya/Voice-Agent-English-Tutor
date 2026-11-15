"""Engine and session factory. SQLite file under data/ (created on first run)."""
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import settings
from .models import Base

_engine = None
_factory = None


def init_db(path: Path | str | None = None):
    """Create the engine and tables. Tests pass a temporary path (or ":memory:")."""
    global _engine, _factory
    path = str(path or settings.db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(_engine, "connect")
    def _sqlite_pragmas(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")

    Base.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    _factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


# Columns added after the first release. create_all() never alters an existing table, so add them here.
LATER_COLUMNS = {"sessions": {"title": "VARCHAR(120)"}, "focus_areas": {"sessions": "INTEGER DEFAULT 0"}}


def _add_missing_columns(engine):
    with engine.begin() as conn:
        for table, cols in LATER_COLUMNS.items():
            have = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, sql_type in cols.items():
                if name not in have:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


@contextmanager
def db() -> Session:
    """`with db() as s: ...` commits on success, rolls back on error."""
    if _factory is None:
        init_db()
    s = _factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
