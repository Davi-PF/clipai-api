"""Add collections_visibility on users and show_on_profile/private privacy on collections

Revision ID: 20261127_0007
Revises: 20261126_0006
Create Date: 2026-11-27 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261127_0007"
down_revision: str | Sequence[str] | None = "20261126_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add profile collection privacy controls and supporting index."""
    op.add_column(
        "users",
        sa.Column(
            "collections_visibility",
            sa.String(length=20),
            server_default=sa.text("'public'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_users_collections_visibility_valid"),
        "users",
        "collections_visibility IN ('public', 'friends', 'private')",
    )

    op.add_column(
        "collections",
        sa.Column(
            "show_on_profile",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.drop_constraint(op.f("ck_collections_privacy_valid"), "collections", type_="check")
    op.create_check_constraint(
        op.f("ck_collections_privacy_valid"),
        "collections",
        "privacy IN ('public', 'friends', 'private', 'unlisted')",
    )
    op.create_index(
        "ix_collections_user_profile",
        "collections",
        ["user_id", "show_on_profile", sa.text("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    """Revert profile collection privacy controls and supporting index."""
    op.drop_index("ix_collections_user_profile", table_name="collections")
    op.execute("UPDATE collections SET privacy = 'unlisted' WHERE privacy = 'private'")
    op.drop_constraint(op.f("ck_collections_privacy_valid"), "collections", type_="check")
    op.create_check_constraint(
        op.f("ck_collections_privacy_valid"),
        "collections",
        "privacy IN ('public', 'friends', 'unlisted')",
    )
    op.drop_column("collections", "show_on_profile")

    op.drop_constraint(op.f("ck_users_collections_visibility_valid"), "users", type_="check")
    op.drop_column("users", "collections_visibility")
