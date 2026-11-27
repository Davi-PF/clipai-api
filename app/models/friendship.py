"""SQLAlchemy 2.0 ORM model for user friendships."""

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class Friendship(TimestampMixin, Base):
    """Bidirectional friendship relation between two users with normalized pair uniqueness."""

    __tablename__ = "friendships"
    __table_args__ = (
        CheckConstraint("requester_id != addressee_id", name="not_self"),
        CheckConstraint("status IN ('pending', 'accepted')", name="status_valid"),
        Index(
            "uq_friendships_user_pair",
            text("LEAST(requester_id, addressee_id)"),
            text("GREATEST(requester_id, addressee_id)"),
            unique=True,
        ),
        Index("ix_friendships_requester_status", "requester_id", "status"),
        Index("ix_friendships_addressee_status", "addressee_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    requester_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    addressee_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )

    requester: Mapped["User"] = relationship("User", foreign_keys=[requester_id])
    addressee: Mapped["User"] = relationship("User", foreign_keys=[addressee_id])
