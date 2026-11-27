"""Add invite_code to users and create friendships and notifications tables

Revision ID: 20261127_0008
Revises: 20261127_0007
Create Date: 2026-11-27 15:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20261127_0008"
down_revision: str | Sequence[str] | None = "20261127_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add users.invite_code with backfill, and create friendships and notifications."""
    # 1. Add invite_code to users (nullable first for backfill)
    op.add_column(
        "users",
        sa.Column("invite_code", sa.String(length=16), nullable=True),
    )
    op.execute(
        """
        UPDATE users
        SET invite_code = substr(
            translate(
                encode(decode(replace(gen_random_uuid()::text, '-', ''), 'hex'), 'base64'),
                '+/=',
                '-_'
            ),
            1,
            12
        )
        WHERE invite_code IS NULL
        """
    )
    op.alter_column(
        "users",
        "invite_code",
        existing_type=sa.String(length=16),
        nullable=False,
    )
    op.create_index("ix_users_invite_code", "users", ["invite_code"], unique=True)

    # 2. Create friendships table
    op.create_table(
        "friendships",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("requester_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("addressee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "requester_id != addressee_id",
            name=op.f("ck_friendships_not_self"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted')",
            name=op.f("ck_friendships_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["requester_id"],
            ["users.id"],
            name=op.f("fk_friendships_requester_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["addressee_id"],
            ["users.id"],
            name=op.f("fk_friendships_addressee_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_friendships")),
    )
    op.create_index(
        "uq_friendships_user_pair",
        "friendships",
        [
            sa.text("LEAST(requester_id, addressee_id)"),
            sa.text("GREATEST(requester_id, addressee_id)"),
        ],
        unique=True,
    )
    op.create_index(
        "ix_friendships_requester_status",
        "friendships",
        ["requester_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_friendships_addressee_status",
        "friendships",
        ["addressee_id", "status"],
        unique=False,
    )

    # 3. Create notifications table
    op.create_table(
        "notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "is_read",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "type IN ('friend_request', 'friend_accepted')",
            name=op.f("ck_notifications_type_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_notifications_actor_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index(
        "ix_notifications_user_read_created",
        "notifications",
        ["user_id", "is_read", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_notifications_user_created",
        "notifications",
        ["user_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_notifications_entity_id",
        "notifications",
        ["entity_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop notifications, friendships, and users.invite_code."""
    op.drop_index("ix_notifications_entity_id", table_name="notifications")
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_index("ix_notifications_user_read_created", table_name="notifications")
    op.drop_table("notifications")

    op.drop_index("ix_friendships_addressee_status", table_name="friendships")
    op.drop_index("ix_friendships_requester_status", table_name="friendships")
    op.drop_index("uq_friendships_user_pair", table_name="friendships")
    op.drop_table("friendships")

    op.drop_index("ix_users_invite_code", table_name="users")
    op.drop_column("users", "invite_code")
