"""personal_lists_and_luna_tasks

Revision ID: c5f8a2d4e917
Revises: a7d3e1c95b24
Create Date: 2026-11-13 03:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5f8a2d4e917"
down_revision: str | Sequence[str] | None = "a7d3e1c95b24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("assistant_family_lists", sa.Column("owner_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_assistant_family_lists_owner_id",
        "assistant_family_lists",
        "assistant_members",
        ["owner_id"],
        ["telegram_id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("assistant_family_lists_name_key", "assistant_family_lists", type_="unique")
    op.create_index(
        "uq_family_list_name",
        "assistant_family_lists",
        ["name"],
        unique=True,
        postgresql_where=sa.text("owner_id IS NULL"),
    )
    op.create_index(
        "uq_personal_list_name",
        "assistant_family_lists",
        ["owner_id", "name"],
        unique=True,
        postgresql_where=sa.text("owner_id IS NOT NULL"),
    )
    op.add_column(
        "assistant_reminders", sa.Column("kind", sa.String(length=10), server_default="remind", nullable=False)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("assistant_reminders", "kind")
    op.execute("DELETE FROM assistant_family_lists WHERE owner_id IS NOT NULL")
    op.drop_index("uq_personal_list_name", table_name="assistant_family_lists")
    op.drop_index("uq_family_list_name", table_name="assistant_family_lists")
    op.create_unique_constraint("assistant_family_lists_name_key", "assistant_family_lists", ["name"])
    op.drop_constraint("fk_assistant_family_lists_owner_id", "assistant_family_lists", type_="foreignkey")
    op.drop_column("assistant_family_lists", "owner_id")
