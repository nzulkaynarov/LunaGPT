"""rename_chores_list

The family's standing chores list was called "Дела", which read like reminders next to the personal
"Мои дела"; it becomes "Дела по дому". Only the list Luna made herself is renamed, with its lines kept.

Revision ID: e4c7b9d2a615
Revises: d2b6f1a8c340
Create Date: 2026-11-14 04:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4c7b9d2a615"
down_revision: str | Sequence[str] | None = "d2b6f1a8c340"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RENAME = sa.text(
    "UPDATE assistant_family_lists SET name = :new WHERE name = :old AND owner_id IS NULL "
    "AND NOT EXISTS (SELECT 1 FROM assistant_family_lists WHERE name = :new AND owner_id IS NULL)"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(RENAME.bindparams(old="Дела", new="Дела по дому"))


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(RENAME.bindparams(old="Дела по дому", new="Дела"))
