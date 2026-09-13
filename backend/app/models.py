import uuid
from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import settings
from .db import Base

# document lifecycle
STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_INDEXED = "indexed"
STATUS_ERROR = "error"
# Suppressed as a near-duplicate of another (better) indexed document.
# Excluded from search/chat/totals like any non-indexed status, but the file
# and row are kept — see ingest/dedupe.py and the /documents/duplicates
# review endpoints.
STATUS_DUPLICATE = "duplicate"
# User cancelled it while it was still queued (before a worker picked it up).
# See /documents/bulk-cancel and pipeline.process_document's early-exit check
# — a job already mid-run can't be safely aborted, so cancel only applies to
# STATUS_QUEUED documents.
STATUS_CANCELLED = "cancelled"


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Document(Base):
    __tablename__ = "documents"
    # `updated_at` below is server-computed (onupdate=func.now()) — without
    # this, the async ORM marks it "expired" after any UPDATE and accessing
    # it (e.g. via Pydantic's model_validate right after a commit) tries to
    # lazily refresh it, which needs an await Pydantic's sync attribute
    # access can't perform ("MissingGreenlet"). This makes SQLAlchemy fetch
    # it via RETURNING in the same statement instead, so it's already
    # populated — no endpoint needs to remember to work around it.
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)

    # file
    original_filename: Mapped[str] = mapped_column(String(512))
    stored_path: Mapped[str] = mapped_column(String(1024))
    canonical_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # classification / extraction
    category: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    doc_type: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    # The single most relevant date for this document (issued/dated/
    # submitted, or a covered period's end date). Replaces the old
    # issuer/issue_date/period_start/period_end/currency/total_amount
    # columns, which turned out unreliable and not universally applicable —
    # issuer/amounts/etc. now live in `facts`, which the extraction prompt
    # captures exhaustively (see ingest/classify.py).
    date: Mapped[date | None] = mapped_column(Date, index=True, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    facts: Mapped[dict] = mapped_column(JSONB, default=dict)

    # content
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # lifecycle
    status: Mapped[str] = mapped_column(String(16), default=STATUS_QUEUED, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # near-duplicate detection (see ingest/dedupe.py)
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), index=True, nullable=True
    )
    duplicate_similarity: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    duplicate_reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    quality_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)

    line_items: Mapped[list["LineItem"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class LineItem(Base):
    __tablename__ = "line_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )

    description: Mapped[str] = mapped_column(Text)
    amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    quantity: Mapped[float | None] = mapped_column(Numeric(14, 3), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    line_date: Mapped[date | None] = mapped_column(Date, index=True, nullable=True)
    # denormalized from the parent document for cheap filtering
    category: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[Document] = relationship(back_populates="line_items")


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embed_dim))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[Document] = relationship(back_populates="chunks")


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __mapper_args__ = {"eager_defaults": True}  # see Document's __mapper_args__ comment

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[ChatSession] = relationship(back_populates="messages")


class Job(Base):
    __tablename__ = "jobs"
    __mapper_args__ = {"eager_defaults": True}  # see Document's __mapper_args__ comment

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="ingest")
    status: Mapped[str] = mapped_column(String(16), default=STATUS_QUEUED, index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
