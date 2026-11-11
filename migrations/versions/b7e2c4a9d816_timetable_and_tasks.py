"""timetable_and_tasks

Revision ID: b7e2c4a9d816
Revises: a3d9e1f7c205
Create Date: 2026-11-11 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "b7e2c4a9d816"
down_revision: str | Sequence[str] | None = "a3d9e1f7c205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "assistant_schedules",
        sa.Column("member_id", sa.BigInteger(), nullable=False),
        sa.Column("days", JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["assistant_members.telegram_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("member_id"),
    )
    op.create_table(
        "assistant_tasks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("member_id", sa.BigInteger(), nullable=False),
        sa.Column("subject", sa.String(length=80), server_default="", nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["assistant_members.telegram_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assistant_tasks_member_id", "assistant_tasks", ["member_id"])
    op.add_column("assistant_reminders", sa.Column("task_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_assistant_reminders_task_id", "assistant_reminders", "assistant_tasks", ["task_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_assistant_reminders_task_id", "assistant_reminders", ["task_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_assistant_reminders_task_id", table_name="assistant_reminders")
    op.drop_constraint("fk_assistant_reminders_task_id", "assistant_reminders", type_="foreignkey")
    op.drop_column("assistant_reminders", "task_id")
    op.drop_index("ix_assistant_tasks_member_id", table_name="assistant_tasks")
    op.drop_table("assistant_tasks")
    op.drop_table("assistant_schedules")
