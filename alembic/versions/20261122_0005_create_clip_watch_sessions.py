"""Create clip_watch_sessions table for video retention telemetry

Revision ID: 20261122_0005
Revises: 20261122_0004
Create Date: 2026-11-22 15:35:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20261122_0005"
down_revision: str | Sequence[str] | None = "20261122_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create clip_watch_sessions table and performance indexes."""
    op.create_table(
        "clip_watch_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("clip_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("viewer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_token", sa.String(length=64), nullable=False),
        sa.Column(
            "watched_seconds",
            sa.Float(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "max_progress_pct",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "reached_buckets",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "completed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "watched_seconds >= 0",
            name=op.f("ck_clip_watch_sessions_watched_seconds_non_negative"),
        ),
        sa.CheckConstraint(
            "max_progress_pct >= 0 AND max_progress_pct <= 100",
            name=op.f("ck_clip_watch_sessions_max_progress_pct_range"),
        ),
        sa.ForeignKeyConstraint(
            ["clip_id"],
            ["clips.id"],
            name=op.f("fk_clip_watch_sessions_clip_id_clips"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["viewer_id"],
            ["users.id"],
            name=op.f("fk_clip_watch_sessions_viewer_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clip_watch_sessions")),
        sa.UniqueConstraint(
            "clip_id",
            "session_token",
            name="uq_clip_watch_sessions_clip_session",
        ),
    )
    op.create_index(
        "ix_clip_watch_sessions_clip_id",
        "clip_watch_sessions",
        ["clip_id"],
        unique=False,
    )
    op.create_index(
        "ix_clip_watch_sessions_clip_created",
        "clip_watch_sessions",
        ["clip_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_clip_watch_sessions_viewer_id",
        "clip_watch_sessions",
        ["viewer_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop clip_watch_sessions table and indexes."""
    op.drop_index("ix_clip_watch_sessions_viewer_id", table_name="clip_watch_sessions")
    op.drop_index("ix_clip_watch_sessions_clip_created", table_name="clip_watch_sessions")
    op.drop_index("ix_clip_watch_sessions_clip_id", table_name="clip_watch_sessions")
    op.drop_table("clip_watch_sessions")
