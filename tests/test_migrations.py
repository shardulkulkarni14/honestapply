"""Migrations must produce exactly the models' schema, and must upgrade the
databases real users already have — including ones created before Alembic.

The drift test is the one that keeps migrations honest: it is easy to change a
model and forget the migration, and then a fresh install (create_all-equivalent,
via head) and an upgraded install silently diverge. Comparing head against the
models catches that at CI time.
"""

from __future__ import annotations

from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from honestapply.db.migrate import (
    BASELINE_REVISION,
    current_revision,
    make_alembic_config,
    run_migrations,
)
from honestapply.db.models import Base


def _head_revision() -> str:
    """The latest migration, read from the scripts — so adding a migration never
    breaks these tests just for pinning a stale revision id."""
    from sqlalchemy import create_engine

    cfg = make_alembic_config(create_engine("sqlite://"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _schema(engine) -> dict[str, dict[str, bool]]:
    """{table: {column: nullable}} minus Alembic's own bookkeeping table."""
    insp = inspect(engine)
    return {
        t: {c["name"]: bool(c["nullable"]) for c in insp.get_columns(t)}
        for t in insp.get_table_names()
        if t != "alembic_version"
    }


def test_head_matches_the_models(tmp_path):
    """ACCEPTANCE: upgrading a fresh DB to head yields the models' own schema.

    If this fails, a model changed without a matching migration.
    """
    mig = create_engine(f"sqlite:///{tmp_path}/mig.db")
    run_migrations(mig)

    models = create_engine(f"sqlite:///{tmp_path}/models.db")
    Base.metadata.create_all(models)

    assert _schema(mig) == _schema(models)


def test_fresh_db_lands_at_head(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/fresh.db")
    run_migrations(engine)
    assert current_revision(engine) == _head_revision()


def test_migrations_are_reversible(tmp_path):
    """Every migration downgrades cleanly back to empty."""
    from alembic import command

    engine = create_engine(f"sqlite:///{tmp_path}/rev.db")
    cfg = make_alembic_config(engine)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    # Nothing of ours should remain (alembic_version may persist, empty).
    remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert remaining == set()


def test_pre_alembic_database_upgrades_in_place(tmp_path):
    """ACCEPTANCE: a database from the create_all era migrates without data loss.

    Such a DB has the baseline tables but no alembic_version row. run_migrations
    must recognise it, stamp the baseline, apply only the newer migrations, and
    leave existing rows intact.
    """
    from alembic import command

    engine = create_engine(f"sqlite:///{tmp_path}/legacy.db")

    # Build the baseline schema, then strip Alembic's marker so it looks exactly
    # like a database that predates Alembic adoption.
    command.upgrade(make_alembic_config(engine), BASELINE_REVISION)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE alembic_version")
        conn.exec_driver_sql(
            "INSERT INTO jobs (id, source_board, external_id, url, url_hash, "
            "company, title, location, status, discovered_at, updated_at) "
            "VALUES (1,'','','','h','Bosch','Eng','DE','applied',"
            "'2026-01-01','2026-01-01')"
        )

    run_migrations(engine)

    insp = inspect(engine)
    assert current_revision(engine) == _head_revision()
    assert "job_events" in insp.get_table_names()
    assert "notes" in {c["name"] for c in insp.get_columns("jobs")}
    with engine.connect() as conn:
        preserved = conn.exec_driver_sql("SELECT company FROM jobs WHERE id=1").scalar()
    assert preserved == "Bosch"


def test_init_db_is_idempotent(tmp_path):
    """Running init_db twice is a no-op the second time, not an error."""
    from honestapply.db import session as sess

    db = tmp_path / "idem.db"
    sess._engine = None  # reset the module-global engine for a clean bind
    sess.init_db(db)
    first = current_revision(create_engine(f"sqlite:///{db}"))
    sess._engine = None
    sess.init_db(db)
    second = current_revision(create_engine(f"sqlite:///{db}"))
    assert first == second == _head_revision()
