"""lifecycle events + running note

Revision ID: 0002_lifecycle_events
Revises: 0001_baseline
Create Date: 2026-08-04

Adds the job_events audit log and a free-form notes column on jobs. The new
post-apply statuses (screening/interviewing/offer/rejected/ghosted) are plain
string values, so they need no schema change — only this table to record when a
job moves between them.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_lifecycle_events"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
    )
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"])
    op.create_index("ix_job_events_at", "job_events", ["at"])

    # SQLite cannot add a column with a table-level rewrite in plain DDL for some
    # cases; batch mode makes ALTER portable. Nullable, so existing rows are fine.
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("notes", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("notes")
    op.drop_index("ix_job_events_at", table_name="job_events")
    op.drop_index("ix_job_events_job_id", table_name="job_events")
    op.drop_table("job_events")
