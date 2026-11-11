"""reminders_and_brief

Revision ID: a3d9e1f7c205
Revises: e7c3a5f1b284
Create Date: 2026-11-11 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3d9e1f7c205"
down_revision: str | Sequence[str] | None = "e7c3a5f1b284"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "assistant_reminders",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("member_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("repeat", sa.String(length=10), server_default="none", nullable=False),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["assistant_members.telegram_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assistant_reminders_member_id", "assistant_reminders", ["member_id"])
    op.create_index("ix_assistant_reminders_due_at", "assistant_reminders", ["due_at"])
    op.add_column("assistant_members", sa.Column("brief_time", sa.String(length=5), nullable=True))
    op.add_column("assistant_members", sa.Column("brief_city", sa.String(length=100), nullable=True))
    op.add_column("assistant_members", sa.Column("brief_lat", sa.Float(), nullable=True))
    op.add_column("assistant_members", sa.Column("brief_lon", sa.Float(), nullable=True))
    op.add_column("assistant_members", sa.Column("brief_sent_on", sa.Date(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    for column in ("brief_sent_on", "brief_lon", "brief_lat", "brief_city", "brief_time"):
        op.drop_column("assistant_members", column)
    op.drop_index("ix_assistant_reminders_due_at", table_name="assistant_reminders")
    op.drop_index("ix_assistant_reminders_member_id", table_name="assistant_reminders")
    op.drop_table("assistant_reminders")
