"""drop_platform_tables

The document RAG, the feedback memory and the Open WebUI feedback sync had no users: 0 documents,
4 test lessons, 5 synced ratings from one day. Their code is gone, and so are their tables. A
nightly backup made before this migration (scripts/backup-postgres.sh) keeps them.

Revision ID: b5e1c7d9a204
Revises: a3d8f5c2e916
Create Date: 2026-11-13 19:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5e1c7d9a204"
down_revision: str | Sequence[str] | None = "a3d8f5c2e916"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("document_chunks", "documents", "feedback_memories", "openwebui_feedback_sync")


def upgrade() -> None:
    """Upgrade schema."""
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("The removed platform tables come back only from a backup: docs/operations.md")
