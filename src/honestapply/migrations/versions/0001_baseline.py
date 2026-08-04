"""baseline — the schema as it stood before Alembic was adopted

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-04

This reproduces exactly what ``Base.metadata.create_all`` produced in the
create_all era: the jobs, applications and run_logs tables. Databases that
predate Alembic are stamped at this revision rather than having it re-run
against them (see honestapply.db.migrate).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_board", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(length=32), nullable=False),
        sa.Column("company", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("location", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("requirements", sa.Text(), nullable=True),
        sa.Column("salary_range_text", sa.String(length=255), nullable=True),
        sa.Column("posted_at", sa.DateTime(), nullable=True),
        sa.Column("discovered_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("ats_type", sa.String(length=32), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("score_reasoning", sa.Text(), nullable=True),
        sa.Column("matched_keywords", sa.Text(), nullable=True),
        sa.Column("gap_flags", sa.Text(), nullable=True),
        sa.Column("matched_resume_path", sa.Text(), nullable=True),
        sa.Column("tailored_resume_path", sa.Text(), nullable=True),
        sa.Column("cover_letter_path", sa.Text(), nullable=True),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("company", "title", "url_hash", name="uq_job_identity"),
    )
    op.create_index("ix_jobs_url_hash", "jobs", ["url_hash"])
    op.create_index("ix_jobs_company", "jobs", ["company"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("applied_at", sa.DateTime(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("confirmation_text", sa.Text(), nullable=True),
        sa.Column("pre_submit_screenshot", sa.Text(), nullable=True),
        sa.Column("post_submit_screenshot", sa.Text(), nullable=True),
        sa.Column("error_log", sa.Text(), nullable=True),
    )
    op.create_index("ix_applications_job_id", "applications", ["job_id"])

    op.create_table(
        "run_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("jobs_processed", sa.Integer(), nullable=False),
        sa.Column("jobs_succeeded", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("run_logs")
    op.drop_index("ix_applications_job_id", table_name="applications")
    op.drop_table("applications")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_jobs_company", table_name="jobs")
    op.drop_index("ix_jobs_url_hash", table_name="jobs")
    op.drop_table("jobs")
