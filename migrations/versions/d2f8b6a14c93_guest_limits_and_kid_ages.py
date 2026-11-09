"""guest_limits_and_kid_ages

Revision ID: d2f8b6a14c93
Revises: c5a1d9e30f72
Create Date: 2026-11-09 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2f8b6a14c93"
down_revision: str | Sequence[str] | None = "c5a1d9e30f72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("assistant_members", sa.Column("birth_year", sa.Integer(), nullable=True))
    op.add_column("assistant_members", sa.Column("access_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("assistant_members", sa.Column("money_limit_usd", sa.Numeric(8, 2), nullable=True))
    op.add_column("assistant_members", sa.Column("spend_month", sa.Date(), nullable=True))
    op.add_column("assistant_members", sa.Column("spent_usd", sa.Numeric(10, 6), server_default="0", nullable=False))
    op.drop_column("assistant_members", "requests_today")
    op.drop_column("assistant_members", "usage_day")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("assistant_members", sa.Column("usage_day", sa.Date(), nullable=True))
    op.add_column("assistant_members", sa.Column("requests_today", sa.Integer(), server_default="0", nullable=False))
    op.drop_column("assistant_members", "spent_usd")
    op.drop_column("assistant_members", "spend_month")
    op.drop_column("assistant_members", "money_limit_usd")
    op.drop_column("assistant_members", "access_until")
    op.drop_column("assistant_members", "birth_year")
