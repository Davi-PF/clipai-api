"""SQLAlchemy 2.0 ORM model for clip watch sessions and video retention telemetry."""

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.clip import Clip
    from app.models.user import User


class ClipWatchSession(TimestampMixin, Base):
    """Stores per-viewer video retention checkpoints and watch duration per clip."""

    __tablename__ = "clip_watch_sessions"
    __table_args__ = (
        UniqueConstraint(
            "clip_id",
            "session_token",
            name="uq_clip_watch_sessions_clip_session",
        ),
        CheckConstraint("watched_seconds >= 0", name="watched_seconds_non_negative"),
        CheckConstraint(
            "max_progress_pct >= 0 AND max_progress_pct <= 100",
            name="max_progress_pct_range",
        ),
        Index("ix_clip_watch_sessions_clip_id", "clip_id"),
        Index("ix_clip_watch_sessions_clip_created", "clip_id", "created_at"),
        Index("ix_clip_watch_sessions_viewer_id", "viewer_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    clip_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clips.id", ondelete="CASCADE"),
        nullable=False,
    )
    viewer_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    session_token: Mapped[str] = mapped_column(String(64), nullable=False)
    watched_seconds: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default=text("0"),
    )
    max_progress_pct: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    reached_buckets: Mapped[list[int]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    completed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )

    clip: Mapped["Clip"] = relationship()
    viewer: Mapped["User | None"] = relationship()
