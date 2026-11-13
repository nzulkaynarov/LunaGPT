"""usage

Counts of what each person uses per day, with model cost, so the owner can see whether Luna is
used at all; no message text is stored.

Revision ID: a3d8f5c2e916
Revises: e4c7b9d2a615
Create Date: 2026-11-13 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3d8f5c2e916"
down_revision: str | Sequence[str] | None = "e4c7b9d2a615"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "assistant_usage",
        sa.Column("member_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("feature", sa.String(length=40), nullable=False),
        sa.Column("count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=10, scale=6), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["assistant_members.telegram_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("member_id", "day", "feature"),
    )
    op.add_column("assistant_members", sa.Column("report_sent_on", sa.Date(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("assistant_members", "report_sent_on")
    op.drop_table("assistant_usage")
