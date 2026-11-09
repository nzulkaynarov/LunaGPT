"""single_entry_applications

Revision ID: b3e7f2a84c61
Revises: a9d4e1c7b250
Create Date: 2026-11-10 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3e7f2a84c61"
down_revision: str | Sequence[str] | None = "a9d4e1c7b250"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "assistant_applications",
        sa.Column("telegram_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_assistant_applications_status", "assistant_applications", ["status"])
    # Guests become permanent, so access that already ended must not come back.
    op.execute("DELETE FROM assistant_members WHERE expires_at IS NOT NULL AND expires_at <= now()")
    op.drop_column("assistant_members", "expires_at")
    op.drop_table("assistant_invites")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table(
        "assistant_invites",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("invite_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("access_hours", sa.Integer(), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("homesec_person_id", sa.Integer(), nullable=True),
    )
    op.add_column("assistant_members", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_table("assistant_applications")
