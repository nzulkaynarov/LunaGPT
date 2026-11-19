"""reply_traces

Luna remembers which Langfuse trace each of her replies came from for 30 days, so a 👍 or 👎 the person puts
on the reply becomes a feedback score on that trace.

Revision ID: c8f2a4d6e913
Revises: b5e1c7d9a204
Create Date: 2026-11-19 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8f2a4d6e913"
down_revision: str | Sequence[str] | None = "b5e1c7d9a204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "assistant_reply_traces",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("trace_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["assistant_members.telegram_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assistant_reply_traces_created_at", "assistant_reply_traces", ["created_at"])
    op.create_index(
        "ix_assistant_reply_traces_message", "assistant_reply_traces", ["chat_id", "message_id"], unique=True
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_assistant_reply_traces_message", table_name="assistant_reply_traces")
    op.drop_index("ix_assistant_reply_traces_created_at", table_name="assistant_reply_traces")
    op.drop_table("assistant_reply_traces")
