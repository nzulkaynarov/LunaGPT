"""cat_stories

Revision ID: f4c9a2e7b158
Revises: e2b7d4f9a613
Create Date: 2026-11-12 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4c9a2e7b158"
down_revision: str | Sequence[str] | None = "e2b7d4f9a613"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "assistant_cat_stories",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.String(length=600), nullable=False),
        sa.Column("photo_file_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=10), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["author_id"], ["assistant_members.telegram_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assistant_cat_stories_author_id", "assistant_cat_stories", ["author_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_assistant_cat_stories_author_id", table_name="assistant_cat_stories")
    op.drop_table("assistant_cat_stories")
