import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class LineItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    description: str
    amount: float | None
    quantity: float | None
    currency: str | None
    line_date: date | None
    category: str | None
    vendor: str | None


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    canonical_filename: str | None
    mime_type: str
    size_bytes: int
    page_count: int | None
    category: str | None
    doc_type: str | None
    date: date | None
    summary: str | None
    facts: dict
    status: str
    error: str | None
    duplicate_of_id: uuid.UUID | None = None
    duplicate_similarity: float | None = None
    quality_score: float | None = None
    created_at: datetime
    updated_at: datetime
    download_url: str | None = None


class DocumentDetail(DocumentOut):
    ocr_text: str | None = None
    line_items: list[LineItemOut] = []


class DocumentListResponse(BaseModel):
    items: list[DocumentOut]
    total: int  # count matching the current filters, ignoring limit/offset — for pagination


class DocumentStats(BaseModel):
    total: int
    indexed: int
    queued: int
    processing: int
    error: int
    duplicate: int
    cancelled: int


class UploadResult(BaseModel):
    # None when no document was created for this entry, e.g. status
    # "too_large" — one bad file in a batch shouldn't abort the rest.
    document_id: uuid.UUID | None = None
    original_filename: str
    status: str
    detail: str | None = None
    # Set only for an exact-byte (sha256) match caught synchronously at
    # upload time. Near-duplicates (a different scan of the same physical
    # document) are found later during processing — see DocumentOut's
    # duplicate_of_id and GET /documents/duplicates.
    duplicate_of: uuid.UUID | None = None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID | None
    kind: str
    status: str
    detail: str | None
    error: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime


class ChatRequest(BaseModel):
    # None for a brand-new chat — the backend creates the session and reports
    # its id back as the first SSE event ({"type": "chat_id", ...}).
    chat_id: uuid.UUID | None = None
    message: str


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    sources: list[dict] = []
    created_at: datetime


class ChatSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime


class ChatSessionDetail(ChatSessionOut):
    messages: list[ChatMessageOut] = []


class SourceDocument(BaseModel):
    document_id: uuid.UUID
    canonical_filename: str | None
    category: str | None
    doc_type: str | None
    date: date | None
    download_url: str


class RenameDocumentRequest(BaseModel):
    filename: str


class BulkDeleteRequest(BaseModel):
    document_ids: list[uuid.UUID]


class BulkDeleteResult(BaseModel):
    deleted: list[uuid.UUID]
    not_found: list[uuid.UUID]


class BulkReprocessResult(BaseModel):
    queued: list[uuid.UUID]
    skipped: list[uuid.UUID]  # not found, or already mid-run


class BulkCancelResult(BaseModel):
    cancelled: list[uuid.UUID]
    skipped: list[uuid.UUID]  # not found, or already past the queued stage


class DuplicatePair(BaseModel):
    duplicate: DocumentOut  # the suppressed (weaker-graded) copy, pending review
    canonical: DocumentOut  # the one currently kept/indexed


class ResolveDuplicateRequest(BaseModel):
    # keep_this: promote the duplicate back to indexed, demote the canonical.
    # keep_other: confirm the current pick; the duplicate stays suppressed.
    # not_duplicate: false positive — both become independently indexed.
    action: Literal["keep_this", "keep_other", "not_duplicate"]
