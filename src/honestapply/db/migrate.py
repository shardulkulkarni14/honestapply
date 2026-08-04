"""Alembic wiring — migrations are how the schema changes from here on.

The project shipped its first releases creating tables with
``Base.metadata.create_all``. That is fine until the schema needs to change under
someone who already has data: ``create_all`` only ever *adds missing tables*, it
never alters an existing one, so a new column would simply never appear and the
app would fail at runtime. Alembic replaces that with ordered, versioned
migrations.

Adopting Alembic on a database that predates it needs one piece of care: those
databases have the tables but no ``alembic_version`` row, so Alembic doesn't know
where they are. :func:`run_migrations` detects that case and stamps them at the
baseline revision before upgrading, so an existing user's data migrates in place
on next launch rather than colliding with the baseline's ``CREATE TABLE``.

Migrations live inside the package (``honestapply/migrations``) so they travel
with the installed wheel; nothing here depends on a repo checkout or a CWD.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# The first migration's revision id. A database created before Alembic was
# adopted is, by definition, exactly at this revision — it has the baseline
# tables — so that is where we stamp it.
BASELINE_REVISION = "0001_baseline"


def make_alembic_config(engine: Engine) -> Config:
    """Build an Alembic config pointed at the packaged migrations and this engine."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    # env.py reads the live engine from here rather than opening its own, so the
    # WAL/busy-timeout pragmas already configured on it are reused.
    cfg.attributes["connection"] = engine
    return cfg


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def run_migrations(engine: Engine) -> None:
    """Bring the database at ``engine`` up to the latest revision.

    Handles three cases: a brand-new empty database (runs every migration), a
    database already under Alembic (runs whatever is outstanding), and a
    pre-Alembic database created by ``create_all`` (stamped at the baseline
    first, so only genuinely new migrations run against it).
    """
    cfg = make_alembic_config(engine)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    pre_alembic = "jobs" in tables and "alembic_version" not in tables
    if pre_alembic:
        # The tables exist but Alembic has never seen this DB — record that it is
        # at the baseline, then let the upgrade apply only later migrations.
        command.stamp(cfg, BASELINE_REVISION)

    command.upgrade(cfg, "head")


def current_revision(engine: Engine) -> str | None:
    """The revision the database is stamped at, or None if unmanaged/empty."""
    return _current_revision(engine)
