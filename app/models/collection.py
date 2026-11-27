"""SQLAlchemy 2.0 ORM models for collections and collection_clips."""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    asc,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.clip import Clip
    from app.models.user import User


class Collection(TimestampMixin, Base):
    """User-curated playlist/collection of clips."""

    __tablename__ = "collections"
    __table_args__ = (
        CheckConstraint(
            "privacy IN ('public', 'friends', 'private', 'unlisted')",
            name="privacy_valid",
        ),
        Index("ix_collections_user_privacy", "user_id", "privacy", desc("created_at")),
        Index(
            "ix_collections_user_profile",
            "user_id",
            "show_on_profile",
            desc("created_at"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    privacy: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="public",
        server_default=text("'public'"),
    )
    show_on_profile: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    accent_color: Mapped[str | None] = mapped_column(String(7), nullable=True)

    owner: Mapped["User"] = relationship()
    clip_links: Mapped[list["CollectionClip"]] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
        order_by="CollectionClip.position",
    )


class CollectionClip(Base):
    """Ordered many-to-many link between collections and clips."""

    __tablename__ = "collection_clips"
    __table_args__ = (
        CheckConstraint("position >= 0", name="position_non_negative"),
        Index("ix_collection_clips_clip_id", "clip_id"),
        Index("ix_collection_clips_order", "collection_id", asc("position")),
    )

    collection_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("collections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    clip_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clips.id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    collection: Mapped["Collection"] = relationship(back_populates="clip_links")
    clip: Mapped["Clip"] = relationship()
