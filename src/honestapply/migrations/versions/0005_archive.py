"""archived_at column on jobs (soft delete)

Revision ID: 0005_archive
Revises: 0004_email_messages
Create Date: 2026-10-09

Adds a nullable ``archived_at`` to jobs so a user can hide a job from the
dashboard's active views without losing it. Additive and nullable — existing
rows are untouched (NULL = active) — so this is a clean in-place upgrade.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_archive"
down_revision: str | None = "0004_email_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("archived_at", sa.DateTime(), nullable=True))
        batch.create_index("ix_jobs_archived_at", ["archived_at"])


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_archived_at")
        batch.drop_column("archived_at")
