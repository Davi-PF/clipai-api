"""Create games, clips, tags and clip_tags tables with initial game seed

Revision ID: 20261121_0002
Revises: 20261121_0001
Create Date: 2026-11-21 23:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20261121_0002"
down_revision: str | Sequence[str] | None = "20261121_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_GAMES: tuple[tuple[str, str], ...] = (
    ("CS2", "cs2"),
    ("Valorant", "valorant"),
    ("League of Legends", "league-of-legends"),
    ("Minecraft", "minecraft"),
    ("Fortnite", "fortnite"),
    ("Rocket League", "rocket-league"),
    ("GTA V", "gta-v"),
    ("Outro", "outro"),
)


def upgrade() -> None:
    """Create games, tags, clips, clip_tags tables, indexes, and seed default games."""
    op.create_table(
        "games",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("cover_url", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_games")),
        sa.UniqueConstraint("name", name=op.f("uq_games_name")),
        sa.UniqueConstraint("slug", name=op.f("uq_games_slug")),
    )

    op.create_table(
        "tags",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=30), nullable=False),
        sa.Column("slug", sa.String(length=30), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tags")),
        sa.UniqueConstraint("name", name=op.f("uq_tags_name")),
        sa.UniqueConstraint("slug", name=op.f("uq_tags_slug")),
    )

    op.create_table(
        "clips",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(length=32), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'pending_upload'"),
            nullable=False,
        ),
        sa.Column(
            "privacy",
            sa.String(length=20),
            server_default=sa.text("'public'"),
            nullable=False,
        ),
        sa.Column("video_object_key", sa.String(length=255), nullable=False),
        sa.Column("thumbnail_object_key", sa.String(length=255), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("duration_seconds", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column("mime_type", sa.String(length=30), nullable=False),
        sa.Column(
            "technical_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("views_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("likes_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("favorites_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
            "status IN ('pending_upload', 'ready', 'failed')",
            name=op.f("ck_clips_status_valid"),
        ),
        sa.CheckConstraint(
            "privacy IN ('public', 'friends', 'unlisted')",
            name=op.f("ck_clips_privacy_valid"),
        ),
        sa.CheckConstraint(
            "file_size_bytes > 0 AND file_size_bytes <= 104857600",
            name=op.f("ck_clips_file_size_limit"),
        ),
        sa.CheckConstraint(
            "duration_seconds >= 1.00 AND duration_seconds <= 120.00",
            name=op.f("ck_clips_duration_limit"),
        ),
        sa.CheckConstraint(
            "mime_type IN ('video/mp4', 'video/webm')",
            name=op.f("ck_clips_mime_type_valid"),
        ),
        sa.CheckConstraint("views_count >= 0", name=op.f("ck_clips_views_non_negative")),
        sa.CheckConstraint("likes_count >= 0", name=op.f("ck_clips_likes_non_negative")),
        sa.CheckConstraint("favorites_count >= 0", name=op.f("ck_clips_favorites_non_negative")),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name=op.f("fk_clips_game_id_games"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_clips_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clips")),
        sa.UniqueConstraint("thumbnail_object_key", name=op.f("uq_clips_thumbnail_object_key")),
        sa.UniqueConstraint("video_object_key", name=op.f("uq_clips_video_object_key")),
    )

    op.create_index("ix_clips_slug", "clips", ["slug"], unique=True)
    op.create_index(
        "ix_clips_public_feed_recent",
        "clips",
        [sa.literal_column("created_at DESC")],
        unique=False,
        postgresql_where=sa.text("status = 'ready' AND privacy = 'public'"),
    )
    op.create_index(
        "ix_clips_public_feed_popular",
        "clips",
        [
            sa.literal_column("likes_count DESC"),
            sa.literal_column("views_count DESC"),
            sa.literal_column("created_at DESC"),
        ],
        unique=False,
        postgresql_where=sa.text("status = 'ready' AND privacy = 'public'"),
    )
    op.create_index(
        "ix_clips_user_id_status_created",
        "clips",
        ["user_id", "status", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_clips_game_id_created",
        "clips",
        ["game_id", sa.literal_column("created_at DESC")],
        unique=False,
        postgresql_where=sa.text("status = 'ready' AND privacy = 'public'"),
    )
    op.create_index(
        "ix_clips_pending_cleanup",
        "clips",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending_upload'"),
    )
    op.create_index(
        "ix_clips_technical_metadata_gin",
        "clips",
        ["technical_metadata"],
        unique=False,
        postgresql_using="gin",
    )

    op.create_table(
        "clip_tags",
        sa.Column("clip_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tag_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["clip_id"],
            ["clips.id"],
            name=op.f("fk_clip_tags_clip_id_clips"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"],
            ["tags.id"],
            name=op.f("fk_clip_tags_tag_id_tags"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("clip_id", "tag_id", name=op.f("pk_clip_tags")),
    )
    op.create_index("ix_clip_tags_tag_id", "clip_tags", ["tag_id"], unique=False)

    # Idempotent initial seed for popular games
    values_sql = ",\n        ".join(f"('{name}', '{slug}')" for name, slug in _SEED_GAMES)
    op.execute(
        f"""
        INSERT INTO games (name, slug)
        VALUES
        {values_sql}
        ON CONFLICT (slug) DO NOTHING
        """
    )


def downgrade() -> None:
    """Drop clip_tags, clips, tags and games tables."""
    op.drop_index("ix_clip_tags_tag_id", table_name="clip_tags")
    op.drop_table("clip_tags")

    op.drop_index("ix_clips_technical_metadata_gin", table_name="clips")
    op.drop_index("ix_clips_pending_cleanup", table_name="clips")
    op.drop_index("ix_clips_game_id_created", table_name="clips")
    op.drop_index("ix_clips_user_id_status_created", table_name="clips")
    op.drop_index("ix_clips_public_feed_popular", table_name="clips")
    op.drop_index("ix_clips_public_feed_recent", table_name="clips")
    op.drop_index("ix_clips_slug", table_name="clips")
    op.drop_table("clips")

    op.drop_table("tags")
    op.drop_table("games")
