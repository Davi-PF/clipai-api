"""SQLAlchemy 2.0 ORM models for clips, tags, and clip_tags."""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.game import Game
    from app.models.user import User


class Tag(Base):
    """Normalized tag entity reusable across clips."""

    __tablename__ = "tags"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ClipTag(Base):
    """Many-to-many association between clips and tags."""

    __tablename__ = "clip_tags"
    __table_args__ = (Index("ix_clip_tags_tag_id", "tag_id"),)

    clip_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clips.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class Clip(TimestampMixin, Base):
    """Gaming video clip entity and R2 object metadata."""

    __tablename__ = "clips"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_upload', 'ready', 'failed')",
            name="status_valid",
        ),
        CheckConstraint(
            "privacy IN ('public', 'friends', 'unlisted')",
            name="privacy_valid",
        ),
        CheckConstraint(
            "file_size_bytes > 0 AND file_size_bytes <= 104857600",
            name="file_size_limit",
        ),
        CheckConstraint(
            "duration_seconds >= 1.00 AND duration_seconds <= 120.00",
            name="duration_limit",
        ),
        CheckConstraint(
            "mime_type IN ('video/mp4', 'video/webm')",
            name="mime_type_valid",
        ),
        CheckConstraint("views_count >= 0", name="views_non_negative"),
        CheckConstraint("likes_count >= 0", name="likes_non_negative"),
        CheckConstraint("favorites_count >= 0", name="favorites_non_negative"),
        Index("ix_clips_slug", "slug", unique=True),
        Index(
            "ix_clips_public_feed_recent",
            desc("created_at"),
            postgresql_where=text("status = 'ready' AND privacy = 'public'"),
        ),
        Index(
            "ix_clips_public_feed_popular",
            desc("likes_count"),
            desc("views_count"),
            desc("created_at"),
            postgresql_where=text("status = 'ready' AND privacy = 'public'"),
        ),
        Index("ix_clips_user_id_status_created", "user_id", "status", desc("created_at")),
        Index(
            "ix_clips_game_id_created",
            "game_id",
            desc("created_at"),
            postgresql_where=text("status = 'ready' AND privacy = 'public'"),
        ),
        Index(
            "ix_clips_pending_cleanup",
            "created_at",
            postgresql_where=text("status = 'pending_upload'"),
        ),
        Index(
            "ix_clips_technical_metadata_gin",
            "technical_metadata",
            postgresql_using="gin",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    slug: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    game_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("games.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending_upload",
        server_default=text("'pending_upload'"),
    )
    privacy: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="public",
        server_default=text("'public'"),
    )
    video_object_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    thumbnail_object_key: Mapped[str | None] = mapped_column(
        String(255),
        unique=True,
        nullable=True,
    )
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_seconds: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(30), nullable=False)
    technical_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    views_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    likes_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    favorites_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )

    author: Mapped["User"] = relationship()
    game: Mapped["Game | None"] = relationship()
    tags: Mapped[list["Tag"]] = relationship(secondary="clip_tags")


class ClipLike(Base):
    """Relational record of user likes on clips."""

    __tablename__ = "clip_likes"
    __table_args__ = (Index("ix_clip_likes_clip_id", "clip_id"),)

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    clip_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clips.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ClipFavorite(Base):
    """Relational record of user-favorited clips."""

    __tablename__ = "clip_favorites"
    __table_args__ = (Index("ix_clip_favorites_user_created", "user_id", desc("created_at")),)

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    clip_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clips.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
