"""family_lists

Revision ID: a7d3e1c95b24
Revises: f4c9a2e7b158
Create Date: 2026-11-13 02:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d3e1c95b24"
down_revision: str | Sequence[str] | None = "f4c9a2e7b158"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "assistant_family_lists",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("name", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "assistant_family_list_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("list_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.String(length=120), nullable=False),
        sa.Column("added_by", sa.BigInteger(), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["list_id"], ["assistant_family_lists.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["added_by"], ["assistant_members.telegram_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assistant_family_list_items_list_id", "assistant_family_list_items", ["list_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_assistant_family_list_items_list_id", table_name="assistant_family_list_items")
    op.drop_table("assistant_family_list_items")
    op.drop_table("assistant_family_lists")
