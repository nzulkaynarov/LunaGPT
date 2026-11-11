"""SQLAlchemy 2.0 ORM models with pgvector support."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lunagpt.db import Base


class Document(Base):
    """Uploaded / Ingested document metadata."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    chunks: Mapped[list["DocumentChunk"]] = relationship(
        "DocumentChunk", back_populates="document", cascade="all, delete-orphan"
    )


class DocumentChunk(Base):
    """Document text chunks with 1024-dim dense vector embedding (bge-m3)."""

    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = mapped_column(Vector(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    document: Mapped[Document] = relationship("Document", back_populates="chunks")

    __table_args__ = (
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class FeedbackMemory(Base):
    """Knowledge base of past user corrections, criticisms, and verified solutions."""

    __tablename__ = "feedback_memories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trace_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    rejected_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_response: Mapped[str] = mapped_column(Text, nullable=False)
    feedback_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)  # 0.0 thumbs down, 1.0 thumbs up
    model_used: Mapped[str] = mapped_column(String(100), default="luna-coder")
    prompt_embedding = mapped_column(Vector(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index(
            "ix_feedback_memories_embedding_hnsw",
            "prompt_embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"prompt_embedding": "vector_cosine_ops"},
        ),
    )


class OpenWebUIFeedbackSync(Base):
    """Open WebUI ratings already forwarded to Langfuse, so each one is sent exactly once."""

    __tablename__ = "openwebui_feedback_sync"

    feedback_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(255), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AssistantMember(Base):
    """A person allowed to talk to Luna AI Assistant."""

    __tablename__ = "assistant_members"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # owner | adult | kid | guest
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)  # E.164, adults only
    birth_year: Mapped[int | None] = mapped_column(Integer, nullable=True)  # children: sets the tone of replies
    access_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # guests only
    money_limit_usd: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)  # guests, per month
    spend_month: Mapped[date | None] = mapped_column(Date, nullable=True)
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), server_default="0", default=Decimal(0), nullable=False)
    homesec_person_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    brief_time: Mapped[str | None] = mapped_column(String(5), nullable=True)  # "07:30" local; None means off
    brief_city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    brief_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    brief_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    brief_sent_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    google_calendar_id: Mapped[str | None] = mapped_column(String(254), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AssistantMemory(Base):
    """A durable fact about one person; removed together with the person."""

    __tablename__ = "assistant_memories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    member_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("assistant_members.telegram_id", ondelete="CASCADE"), nullable=False, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = mapped_column(Vector(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index(
            "ix_assistant_memories_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class AssistantFamilyMemory(Base):
    """A note one family member asked to keep for the whole family; guests never reach this table."""

    __tablename__ = "assistant_family_memories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = mapped_column(Vector(1024), nullable=False)
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index(
            "ix_assistant_family_memories_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class AssistantMessage(Base):
    """The last turns of a conversation, kept so a restart does not interrupt it."""

    __tablename__ = "assistant_messages"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("assistant_members.telegram_id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AssistantApplication(Base):
    """Somebody who asked to join: waiting, turned away, or never answered."""

    __tablename__ = "assistant_applications"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)  # E.164, kept for good
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # pending | rejected | expired
    decided_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AssistantTimeRequest(Base):
    """A child's pending screen-time request, tracked so Luna can report the decision."""

    __tablename__ = "assistant_time_requests"

    request_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AssistantReminder(Base):
    """A personal reminder; a repeating one moves its due time forward after each ring."""

    __tablename__ = "assistant_reminders"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("assistant_members.telegram_id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    repeat: Mapped[str] = mapped_column(String(10), nullable=False, server_default="none")
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    task_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("assistant_tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AssistantSchedule(Base):
    """A person's weekly timetable: {"mon": ["математика", ...], ...}."""

    __tablename__ = "assistant_schedules"

    member_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("assistant_members.telegram_id", ondelete="CASCADE"), primary_key=True
    )
    days: Mapped[dict[str, list[str]]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AssistantTask(Base):
    """Homework or an adult's task, with the day it is due when one is known."""

    __tablename__ = "assistant_tasks"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("assistant_members.telegram_id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject: Mapped[str] = mapped_column(String(80), nullable=False, server_default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
