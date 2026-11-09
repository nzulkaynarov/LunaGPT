"""keep_phone_numbers

Revision ID: c5a1d9e30f72
Revises: b3e7f2a84c61
Create Date: 2026-11-09 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5a1d9e30f72"
down_revision: str | Sequence[str] | None = "b3e7f2a84c61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("assistant_members", sa.Column("phone", sa.String(32), nullable=True))
    op.create_index("ix_assistant_members_phone", "assistant_members", ["phone"])
    op.create_index("ix_assistant_applications_phone", "assistant_applications", ["phone"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_assistant_applications_phone", table_name="assistant_applications")
    op.drop_index("ix_assistant_members_phone", table_name="assistant_members")
    op.drop_column("assistant_members", "phone")
