"""Create clip_views table and temporal engagement indexes

Revision ID: 20261126_0006
Revises: 20261122_0005
Create Date: 2026-11-26 15:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20261126_0006"
down_revision: str | Sequence[str] | None = "20261122_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create clip_views table and temporal indexes for engagement queries."""
    op.create_table(
        "clip_views",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("clip_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("viewer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["clip_id"],
            ["clips.id"],
            name=op.f("fk_clip_views_clip_id_clips"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["viewer_id"],
            ["users.id"],
            name=op.f("fk_clip_views_viewer_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clip_views")),
    )
    op.create_index(
        "ix_clip_views_clip_created",
        "clip_views",
        ["clip_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_clip_views_created_clip",
        "clip_views",
        ["created_at", "clip_id"],
        unique=False,
    )
    op.create_index(
        "ix_clip_likes_created_clip",
        "clip_likes",
        ["created_at", "clip_id"],
        unique=False,
    )
    op.create_index(
        "ix_clip_favorites_created_clip",
        "clip_favorites",
        ["created_at", "clip_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop temporal engagement indexes and clip_views table."""
    op.drop_index("ix_clip_favorites_created_clip", table_name="clip_favorites")
    op.drop_index("ix_clip_likes_created_clip", table_name="clip_likes")
    op.drop_index("ix_clip_views_created_clip", table_name="clip_views")
    op.drop_index("ix_clip_views_clip_created", table_name="clip_views")
    op.drop_table("clip_views")
