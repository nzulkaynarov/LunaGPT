"""member_seen_version

Revision ID: d2b6f1a8c340
Revises: c5f8a2d4e917
Create Date: 2026-11-13 04:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2b6f1a8c340"
down_revision: str | Sequence[str] | None = "c5f8a2d4e917"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("assistant_members", sa.Column("seen_version", sa.String(length=16), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("assistant_members", "seen_version")
