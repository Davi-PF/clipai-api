"""Add user profile avatar, Discord OAuth2 and 2FA backup codes columns

Revision ID: 20261122_0004
Revises: 20261122_0003
Create Date: 2026-11-22 13:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20261122_0004"
down_revision: str | Sequence[str] | None = "20261122_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Update users table with profile avatar R2 key, Discord OAuth2, and 2FA backup codes."""
    op.alter_column(
        "users",
        "password_hash",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.alter_column(
        "users",
        "display_name",
        existing_type=sa.String(length=60),
        type_=sa.String(length=50),
        existing_nullable=True,
        postgresql_using="SUBSTRING(display_name FROM 1 FOR 50)",
    )
    op.alter_column(
        "users",
        "bio",
        existing_type=sa.Text(),
        type_=sa.String(length=240),
        existing_nullable=True,
        postgresql_using="SUBSTRING(bio FROM 1 FOR 240)",
    )

    op.add_column("users", sa.Column("avatar_key", sa.String(length=255), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "avatar_source",
            sa.String(length=20),
            server_default=sa.text("'none'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_users_avatar_source_valid"),
        "users",
        "avatar_source IN ('custom', 'discord', 'none')",
    )

    op.add_column("users", sa.Column("discord_id", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("discord_username", sa.String(length=100), nullable=True))
    op.add_column("users", sa.Column("discord_avatar_url", sa.String(length=512), nullable=True))
    op.add_column(
        "users",
        sa.Column("discord_linked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_discord_id", "users", ["discord_id"], unique=True)

    op.add_column(
        "users",
        sa.Column(
            "totp_backup_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Revert profile avatar, Discord OAuth2, and 2FA backup codes columns from users."""
    op.drop_column("users", "totp_backup_codes")

    op.drop_index("ix_users_discord_id", table_name="users")
    op.drop_column("users", "discord_linked_at")
    op.drop_column("users", "discord_avatar_url")
    op.drop_column("users", "discord_username")
    op.drop_column("users", "discord_id")

    op.drop_constraint(op.f("ck_users_avatar_source_valid"), "users", type_="check")
    op.drop_column("users", "avatar_source")
    op.drop_column("users", "avatar_key")

    op.alter_column(
        "users",
        "bio",
        existing_type=sa.String(length=240),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "users",
        "display_name",
        existing_type=sa.String(length=50),
        type_=sa.String(length=60),
        existing_nullable=True,
    )
    op.execute("UPDATE users SET password_hash = '' WHERE password_hash IS NULL")
    op.alter_column(
        "users",
        "password_hash",
        existing_type=sa.String(length=255),
        nullable=False,
    )
