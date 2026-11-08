"""member_homesec_person

Revision ID: f1c8d3e6a472
Revises: e7a4c2d9f1b3
Create Date: 2026-11-09 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1c8d3e6a472"
down_revision: str | Sequence[str] | None = "e7a4c2d9f1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("assistant_members", sa.Column("homesec_person_id", sa.Integer(), nullable=True))
    op.add_column("assistant_invites", sa.Column("homesec_person_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("assistant_invites", "homesec_person_id")
    op.drop_column("assistant_members", "homesec_person_id")
