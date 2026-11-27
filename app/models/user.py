"""SQLAlchemy 2.0 ORM models for authentication and user accounts."""

import secrets
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


def generate_invite_code() -> str:
    """Generate a 12-character URL-safe random invite code (fits String(16))."""
    return secrets.token_urlsafe(9)


class User(TimestampMixin, Base):
    """User account model aligned with docs/DATABASE.md."""

    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_lower_email", text("LOWER(email)"), unique=True),
        Index("ix_users_lower_username", text("LOWER(username)"), unique=True),
        Index("ix_users_discord_id", "discord_id", unique=True),
        Index("ix_users_invite_code", "invite_code", unique=True),
        CheckConstraint(
            "avatar_source IN ('custom', 'discord', 'none')",
            name="avatar_source_valid",
        ),
        CheckConstraint(
            "collections_visibility IN ('public', 'friends', 'private')",
            name="collections_visibility_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    username: Mapped[str] = mapped_column(String(30), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    avatar_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="none",
        server_default=text("'none'"),
    )
    bio: Mapped[str | None] = mapped_column(String(240), nullable=True)
    collections_visibility: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="public",
        server_default=text("'public'"),
    )
    invite_code: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=generate_invite_code,
    )
    is_email_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    # Discord OAuth2 integration
    discord_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discord_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    discord_avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    discord_linked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # 2FA TOTP (PyOTP + Fernet)
    is_2fa_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    totp_secret_encrypted: Mapped[str | None] = mapped_column(String(512), nullable=True)
    totp_backup_codes: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    @property
    def hashed_password(self) -> str | None:
        """Return stored Argon2id password hash (or None for Discord-only accounts)."""
        return self.password_hash

    @hashed_password.setter
    def hashed_password(self, value: str | None) -> None:
        """Assign Argon2id password hash."""
        self.password_hash = value

    @property
    def has_password(self) -> bool:
        """Return True when the account has a local password configured."""
        return bool(self.password_hash)

    @property
    def discord_linked(self) -> bool:
        """Return True when a Discord account is linked to this user."""
        return bool(self.discord_id)


class RefreshToken(Base):
    """Rotating refresh token session record stored as SHA-256 hash."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_token_hash", "token_hash", unique=True),
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_family_id", "family_id"),
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
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    family_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    replaced_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("refresh_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")

    @property
    def is_revoked(self) -> bool:
        """Return True if this refresh token has been revoked or rotated."""
        return self.revoked_at is not None
