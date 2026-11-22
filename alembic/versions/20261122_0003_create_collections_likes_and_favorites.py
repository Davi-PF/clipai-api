"""Create collections, collection_clips, clip_likes and clip_favorites tables

Revision ID: 20261122_0003
Revises: 20261121_0002
Create Date: 2026-11-22 01:42:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20261122_0003"
down_revision: str | Sequence[str] | None = "20261121_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create collections, collection_clips, clip_likes and clip_favorites tables."""
    op.create_table(
        "collections",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "privacy",
            sa.String(length=20),
            server_default=sa.text("'public'"),
            nullable=False,
        ),
        sa.Column("accent_color", sa.String(length=7), nullable=True),
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
            "privacy IN ('public', 'friends', 'unlisted')",
            name=op.f("ck_collections_privacy_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_collections_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collections")),
    )
    op.create_index(
        "ix_collections_user_privacy",
        "collections",
        ["user_id", "privacy", sa.literal_column("created_at DESC")],
        unique=False,
    )

    op.create_table(
        "collection_clips",
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("clip_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("position >= 0", name=op.f("ck_collection_clips_position_non_negative")),
        sa.ForeignKeyConstraint(
            ["clip_id"],
            ["clips.id"],
            name=op.f("fk_collection_clips_clip_id_clips"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            name=op.f("fk_collection_clips_collection_id_collections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("collection_id", "clip_id", name=op.f("pk_collection_clips")),
    )
    op.create_index("ix_collection_clips_clip_id", "collection_clips", ["clip_id"], unique=False)
    op.create_index(
        "ix_collection_clips_order",
        "collection_clips",
        ["collection_id", sa.literal_column("position ASC")],
        unique=False,
    )

    op.create_table(
        "clip_likes",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("clip_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["clip_id"],
            ["clips.id"],
            name=op.f("fk_clip_likes_clip_id_clips"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_clip_likes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "clip_id", name=op.f("pk_clip_likes")),
    )
    op.create_index("ix_clip_likes_clip_id", "clip_likes", ["clip_id"], unique=False)

    op.create_table(
        "clip_favorites",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("clip_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["clip_id"],
            ["clips.id"],
            name=op.f("fk_clip_favorites_clip_id_clips"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_clip_favorites_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "clip_id", name=op.f("pk_clip_favorites")),
    )
    op.create_index(
        "ix_clip_favorites_user_created",
        "clip_favorites",
        ["user_id", sa.literal_column("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    """Drop clip_favorites, clip_likes, collection_clips and collections tables."""
    op.drop_index("ix_clip_favorites_user_created", table_name="clip_favorites")
    op.drop_table("clip_favorites")

    op.drop_index("ix_clip_likes_clip_id", table_name="clip_likes")
    op.drop_table("clip_likes")

    op.drop_index("ix_collection_clips_order", table_name="collection_clips")
    op.drop_index("ix_collection_clips_clip_id", table_name="collection_clips")
    op.drop_table("collection_clips")

    op.drop_index("ix_collections_user_privacy", table_name="collections")
    op.drop_table("collections")
