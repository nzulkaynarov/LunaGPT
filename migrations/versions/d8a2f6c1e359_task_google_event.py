"""task_google_event

Revision ID: d8a2f6c1e359
Revises: c5f1a8e3b947
Create Date: 2026-11-11 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8a2f6c1e359"
down_revision: str | Sequence[str] | None = "c5f1a8e3b947"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("assistant_tasks", sa.Column("google_event_id", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("assistant_tasks", "google_event_id")
