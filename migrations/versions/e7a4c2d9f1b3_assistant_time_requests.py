"""assistant_time_requests

Revision ID: e7a4c2d9f1b3
Revises: d4e2b7c1a930
Create Date: 2026-11-09 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7a4c2d9f1b3"
down_revision: str | Sequence[str] | None = "d4e2b7c1a930"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "assistant_time_requests",
        sa.Column("request_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("request_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("assistant_time_requests")
