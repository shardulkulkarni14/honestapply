"""email_messages: Gmail inbox sync

Revision ID: 0004_email_messages
Revises: 0003_taxonomy
Create Date: 2026-10-05

Adds the email_messages table backing `honestapply inbox` (Gmail sync). A row is
one ingested recruiter/ATS email: unique on gmail_msg_id (classify once), with a
nullable job_id link. Status changes an email causes are recorded in job_events
with source='email' — this table only stores what was seen, so no enum/status
schema change is needed.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_email_messages"
down_revision: str | None = "0003_taxonomy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("gmail_msg_id", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("sender", sa.String(length=320), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.Column("classification", sa.String(length=32), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_email_messages_job_id", "email_messages", ["job_id"])
    op.create_index(
        "ix_email_messages_gmail_msg_id", "email_messages", ["gmail_msg_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_email_messages_gmail_msg_id", table_name="email_messages")
    op.drop_index("ix_email_messages_job_id", table_name="email_messages")
    op.drop_table("email_messages")
