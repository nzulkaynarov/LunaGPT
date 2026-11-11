"""member_google_calendar

Revision ID: c5f1a8e3b947
Revises: b7e2c4a9d816
Create Date: 2026-11-11 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5f1a8e3b947"
down_revision: str | Sequence[str] | None = "b7e2c4a9d816"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("assistant_members", sa.Column("google_calendar_id", sa.String(length=254), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("assistant_members", "google_calendar_id")
