from pathlib import Path
import os

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, text, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import sessionmaker

from app.config import ROOT, Settings
from app.models import SystemState

SCHEMA_HEAD = "0006_group_workflows"


def make_engine(settings: Settings) -> Engine:
    url = make_url(settings.database_url.get_secret_value())
    if url.drivername.startswith("sqlite") and url.database not in (None, "", ":memory:"):
        path=Path(url.database).resolve()
        path.parent.mkdir(parents=True, exist_ok=True,mode=0o700)
        if not path.exists():
            try:os.close(os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600))
            except FileExistsError:pass
    engine = create_engine(
        url, pool_pre_ping=True, hide_parameters=True,
        connect_args={"check_same_thread": False, "timeout": 15} if url.drivername.startswith("sqlite") else {},
    )
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def sqlite_pragmas(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=15000")
            if connection.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
                connection.execute('PRAGMA journal_mode=WAL')
    return engine


def migrations_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def migrate(engine: Engine, target: str = "head") -> None:
    with engine.begin() as connection:
        cfg = migrations_config()
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, target)


def session_factory(engine: Engine):
    return sessionmaker(engine, expire_on_commit=False)


def start_write(db, engine: Engine):
    # SQLite reserves its writer before authentication reads. PostgreSQL locks one
    # permanent row. Account lifecycle checks and session invalidation then share
    # a transaction, including across multiple server processes.
    if engine.dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    else:
        db.execute(update(SystemState).where(SystemState.id == 1).values(revision=SystemState.revision + 1))

