"""taxonomy columns on jobs

Revision ID: 0003_taxonomy
Revises: 0002_lifecycle_events
Create Date: 2026-08-04

Adds role_family, seniority, work_model and employer_tier to jobs. All nullable
and additive — existing rows are untouched — so this is a clean in-place upgrade.
These are grouping dimensions for analytics and inputs to prefilter.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_taxonomy"
down_revision: str | None = "0002_lifecycle_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("role_family", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("seniority", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("work_model", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("employer_tier", sa.String(length=16), nullable=True))
        batch.create_index("ix_jobs_role_family", ["role_family"])


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_role_family")
        batch.drop_column("employer_tier")
        batch.drop_column("work_model")
        batch.drop_column("seniority")
        batch.drop_column("role_family")
