"""openwebui_feedback_sync

Revision ID: c3f1a9d27e10
Revises: b81616f50ded
Create Date: 2026-11-08 23:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3f1a9d27e10"
down_revision: str | Sequence[str] | None = "b81616f50ded"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "openwebui_feedback_sync",
        sa.Column("feedback_id", sa.String(length=255), nullable=False),
        sa.Column("trace_id", sa.String(length=255), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("feedback_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("openwebui_feedback_sync")
