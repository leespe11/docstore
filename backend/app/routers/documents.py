from __future__ import annotations

import mimetypes
import os
import tempfile
import uuid
import zipfile
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import Text, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import get_session
from ..models import (
    STATUS_CANCELLED,
    STATUS_DUPLICATE,
    STATUS_INDEXED,
    STATUS_PROCESSING,
    STATUS_QUEUED,
    Document,
    Job,
)
from ..queue import enqueue_process_document
from ..schemas import (
    BulkCancelResult,
    BulkDeleteRequest,
    BulkDeleteResult,
    BulkReprocessResult,
    DocumentDetail,
    DocumentListResponse,
    DocumentOut,
    DocumentStats,
    DuplicatePair,
    RenameDocumentRequest,
    ResolveDuplicateRequest,
    UploadResult,
)
from ..storage import (
    absolute_path,
    sanitize_filename,
    save_incoming,
    sha256_bytes,
    storage_relpath,
)

router = APIRouter(prefix="/documents", tags=["documents"])


def _to_out(doc: Document) -> DocumentOut:
    out = DocumentOut.model_validate(doc)
    out.download_url = f"/documents/{doc.id}/download"
    return out


@router.post("", response_model=list[UploadResult])
async def upload_documents(
    files: list[UploadFile],
    session: AsyncSession = Depends(get_session),
) -> list[UploadResult]:
    results: list[UploadResult] = []
    max_bytes = settings.max_upload_mb * 1024 * 1024

    for f in files:
        filename = f.filename or "unknown"
        data = await f.read()
        if len(data) > max_bytes:
            # Skip, don't raise: one oversized file in a large batch
            # shouldn't abort every file after it.
            results.append(
                UploadResult(
                    original_filename=filename,
                    status="too_large",
                    detail=f"{len(data) / 1024 / 1024:.1f}MB exceeds the {settings.max_upload_mb}MB limit",
                )
            )
            continue

        digest = sha256_bytes(data)
        existing = (
            await session.execute(select(Document).where(Document.sha256 == digest))
        ).scalars().first()
        if existing:
            results.append(
                UploadResult(
                    document_id=existing.id,
                    original_filename=filename,
                    status="duplicate",
                    duplicate_of=existing.id,
                )
            )
            continue

        mime_type = f.content_type
        if not mime_type or mime_type == "application/octet-stream":
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        dest = save_incoming(filename, data)
        doc = Document(
            original_filename=filename if filename != "unknown" else dest.name,
            stored_path=storage_relpath(dest),
            mime_type=mime_type,
            sha256=digest,
            size_bytes=len(data),
            status=STATUS_QUEUED,
        )
        session.add(doc)
        await session.flush()

        job = Job(document_id=doc.id, kind="ingest", status=STATUS_QUEUED)
        session.add(job)
        await session.commit()

        await enqueue_process_document(str(doc.id), str(job.id))
        results.append(UploadResult(document_id=doc.id, original_filename=doc.original_filename, status=doc.status))

    return results


_SORT_COLUMNS = {
    "name": func.coalesce(Document.canonical_filename, Document.original_filename),
    "uploaded": Document.created_at,
    "modified": Document.updated_at,
    "document_date": Document.date,
    "category": Document.category,
    "doc_type": Document.doc_type,
    "status": Document.status,
}


def _apply_filters(
    stmt,
    *,
    category: str | None,
    doc_type: str | None,
    status: str | None,
    q: str | None,
    date_from: str | None,
    date_to: str | None,
):
    if category:
        stmt = stmt.where(Document.category.ilike(f"%{category}%"))
    if doc_type:
        stmt = stmt.where(Document.doc_type.ilike(f"%{doc_type}%"))
    if status:
        stmt = stmt.where(Document.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            Document.original_filename.ilike(like)
            | Document.canonical_filename.ilike(like)
            | Document.summary.ilike(like)
            | cast(Document.facts, Text).ilike(like)
        )
    if date_from:
        stmt = stmt.where(Document.date >= datetime.strptime(date_from, "%Y-%m-%d").date())
    if date_to:
        stmt = stmt.where(Document.date <= datetime.strptime(date_to, "%Y-%m-%d").date())
    return stmt


@router.get("", response_model=DocumentListResponse)
async def list_documents_endpoint(
    category: str | None = None,
    doc_type: str | None = None,
    status: str | None = None,
    q: str | None = Query(None, description="substring match on filename/summary/facts"),
    date_from: str | None = None,
    date_to: str | None = None,
    sort_by: str = Query("uploaded", pattern="^(uploaded|name|modified|document_date|category|doc_type|status)$"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = 100,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> DocumentListResponse:
    base = _apply_filters(
        select(Document), category=category, doc_type=doc_type, status=status, q=q, date_from=date_from, date_to=date_to
    )

    count_stmt = _apply_filters(
        select(func.count()).select_from(Document),
        category=category, doc_type=doc_type, status=status, q=q, date_from=date_from, date_to=date_to,
    )
    total = (await session.execute(count_stmt)).scalar_one()

    sort_col = _SORT_COLUMNS[sort_by]
    order = sort_col.asc() if sort_dir == "asc" else sort_col.desc()
    if sort_by in ("document_date", "category", "doc_type"):
        order = order.nullslast()
    stmt = base.order_by(order).limit(min(limit, 500)).offset(max(offset, 0))

    docs = (await session.execute(stmt)).scalars().all()
    return DocumentListResponse(items=[_to_out(d) for d in docs], total=total)


@router.get("/stats", response_model=DocumentStats)
async def document_stats(session: AsyncSession = Depends(get_session)) -> DocumentStats:
    """Global counts across ALL documents (unfiltered) for the top-bar status
    cluster — deliberately ignores any search/filter the documents table is
    currently applying, so it always reflects overall system health."""
    rows = (await session.execute(select(Document.status, func.count()).group_by(Document.status))).all()
    counts = {status: n for status, n in rows}
    return DocumentStats(
        total=sum(counts.values()),
        indexed=counts.get(STATUS_INDEXED, 0),
        queued=counts.get(STATUS_QUEUED, 0),
        processing=counts.get(STATUS_PROCESSING, 0),
        error=counts.get("error", 0),
        duplicate=counts.get(STATUS_DUPLICATE, 0),
        cancelled=counts.get(STATUS_CANCELLED, 0),
    )


@router.get("/categories", response_model=list[str])
async def list_categories(session: AsyncSession = Depends(get_session)) -> list[str]:
    """Distinct categories in use, for populating the documents table's
    category filter dropdown. Read-only — there's no category-editing
    feature; this just lists what ingestion has already filed things under."""
    rows = (
        await session.execute(select(Document.category).where(Document.category.isnot(None)).distinct())
    ).scalars().all()
    return sorted({c for c in rows if c})


@router.post("/bulk-delete", response_model=BulkDeleteResult)
async def bulk_delete_documents(
    payload: BulkDeleteRequest, session: AsyncSession = Depends(get_session)
) -> BulkDeleteResult:
    deleted: list[uuid.UUID] = []
    not_found: list[uuid.UUID] = []
    paths = []

    for doc_id in payload.document_ids:
        doc = await session.get(Document, doc_id)
        if doc is None:
            not_found.append(doc_id)
            continue
        paths.append(absolute_path(doc.stored_path))
        await session.delete(doc)
        deleted.append(doc_id)

    await session.commit()
    for path in paths:
        if path.exists():
            path.unlink(missing_ok=True)

    return BulkDeleteResult(deleted=deleted, not_found=not_found)


@router.post("/bulk-reprocess", response_model=BulkReprocessResult)
async def bulk_reprocess_documents(
    payload: BulkDeleteRequest, session: AsyncSession = Depends(get_session)
) -> BulkReprocessResult:
    """Re-run OCR -> extraction -> chunk/embed for each selected document
    against the file already on disk — the bulk version of the per-row ♻️
    reprocess action, for backfilling fixes onto documents ingested before
    they existed."""
    queued: list[uuid.UUID] = []
    skipped: list[uuid.UUID] = []

    for doc_id in payload.document_ids:
        doc = await session.get(Document, doc_id)
        if doc is None or doc.status == STATUS_PROCESSING or not absolute_path(doc.stored_path).exists():
            skipped.append(doc_id)
            continue

        doc.status = STATUS_QUEUED
        doc.error = None
        job = Job(document_id=doc.id, kind="reprocess", status=STATUS_QUEUED)
        session.add(job)
        await session.commit()

        await enqueue_process_document(str(doc.id), str(job.id))
        queued.append(doc_id)

    return BulkReprocessResult(queued=queued, skipped=skipped)


@router.post("/bulk-cancel", response_model=BulkCancelResult)
async def bulk_cancel_documents(
    payload: BulkDeleteRequest, session: AsyncSession = Depends(get_session)
) -> BulkCancelResult:
    """Cancel documents still waiting in the queue. A document already being
    processed can't be safely interrupted mid-OCR, so it's skipped, not
    cancelled — see pipeline.process_document's matching early-exit check."""
    cancelled: list[uuid.UUID] = []
    skipped: list[uuid.UUID] = []

    for doc_id in payload.document_ids:
        doc = await session.get(Document, doc_id)
        if doc is None or doc.status != STATUS_QUEUED:
            skipped.append(doc_id)
            continue

        job_stmt = (
            select(Job)
            .where(Job.document_id == doc_id, Job.status == STATUS_QUEUED)
            .order_by(Job.created_at.desc())
        )
        job = (await session.execute(job_stmt)).scalars().first()
        if job is not None:
            job.status = STATUS_CANCELLED
        doc.status = STATUS_CANCELLED
        await session.commit()
        cancelled.append(doc_id)

    return BulkCancelResult(cancelled=cancelled, skipped=skipped)


@router.post("/download-zip")
async def download_zip(
    background_tasks: BackgroundTasks,
    document_ids: list[uuid.UUID] = Body(..., embed=True),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    docs = []
    for doc_id in document_ids:
        doc = await session.get(Document, doc_id)
        if doc is not None:
            docs.append(doc)
    if not docs:
        raise HTTPException(404, "no matching documents")

    fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    used_names: set[str] = set()
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc in docs:
            path = absolute_path(doc.stored_path)
            if not path.exists():
                continue
            name = doc.canonical_filename or doc.original_filename
            # Zip entries must be unique — two documents can share a display
            # name (e.g. after a rename collision elsewhere), so disambiguate.
            final_name = name
            n = 1
            while final_name in used_names:
                stem, ext = os.path.splitext(name)
                final_name = f"{stem} ({n}){ext}"
                n += 1
            used_names.add(final_name)
            zf.write(path, arcname=final_name)

    # FastAPI attaches this injected BackgroundTasks instance to whatever
    # Response the endpoint returns and runs it after the response is sent —
    # no need to pass `background=` to FileResponse explicitly.
    background_tasks.add_task(os.unlink, tmp_path)
    return FileResponse(tmp_path, filename="documents.zip", media_type="application/zip")


@router.get("/duplicates", response_model=list[DuplicatePair])
async def list_duplicates(session: AsyncSession = Depends(get_session)) -> list[DuplicatePair]:
    """Pending near-duplicate flags awaiting your review. Each pairs the
    suppressed (weaker-graded) copy with the one currently kept/indexed."""
    stmt = (
        select(Document)
        .where(Document.status == STATUS_DUPLICATE, Document.duplicate_reviewed.is_(False))
        .order_by(Document.created_at.desc())
    )
    dupes = (await session.execute(stmt)).scalars().all()

    pairs: list[DuplicatePair] = []
    for d in dupes:
        if d.duplicate_of_id is None:
            continue
        canonical = await session.get(Document, d.duplicate_of_id)
        if canonical is None:
            continue
        pairs.append(DuplicatePair(duplicate=_to_out(d), canonical=_to_out(canonical)))
    return pairs


@router.post("/{document_id}/resolve-duplicate", response_model=DocumentOut)
async def resolve_duplicate(
    document_id: uuid.UUID,
    payload: ResolveDuplicateRequest,
    session: AsyncSession = Depends(get_session),
) -> DocumentOut:
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    if doc.status != STATUS_DUPLICATE or doc.duplicate_of_id is None:
        raise HTTPException(400, "document is not a pending duplicate")

    other = await session.get(Document, doc.duplicate_of_id)
    if other is None:
        raise HTTPException(404, "the document this was matched against no longer exists")

    if payload.action == "keep_this":
        doc.status = STATUS_INDEXED
        doc.duplicate_of_id = None
        doc.duplicate_reviewed = True
        other.status = STATUS_DUPLICATE
        other.duplicate_of_id = doc.id
        other.duplicate_similarity = doc.duplicate_similarity
        other.duplicate_reviewed = True
    elif payload.action == "keep_other":
        doc.duplicate_reviewed = True  # stays suppressed; just clears it from the review queue
    else:  # not_duplicate
        doc.status = STATUS_INDEXED
        doc.duplicate_of_id = None
        doc.duplicate_reviewed = True

    await session.commit()
    return _to_out(doc)


@router.get("/{document_id}", response_model=DocumentDetail)
async def get_document_endpoint(document_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> DocumentDetail:
    stmt = select(Document).options(selectinload(Document.line_items)).where(Document.id == document_id)
    doc = (await session.execute(stmt)).scalars().first()
    if doc is None:
        raise HTTPException(404, "document not found")
    detail = DocumentDetail.model_validate(doc)
    detail.download_url = f"/documents/{doc.id}/download"
    return detail


@router.patch("/{document_id}", response_model=DocumentOut)
async def rename_document(
    document_id: uuid.UUID,
    payload: RenameDocumentRequest,
    session: AsyncSession = Depends(get_session),
) -> DocumentOut:
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(404, "document not found")

    old_path = absolute_path(doc.stored_path)
    new_name = sanitize_filename(payload.filename, fallback_ext=old_path.suffix)
    new_path = old_path.with_name(new_name)

    if new_path != old_path:
        if not old_path.exists():
            raise HTTPException(410, "file missing from storage")
        if new_path.exists():
            raise HTTPException(409, f"'{new_name}' already exists in this folder")
        old_path.rename(new_path)
        doc.stored_path = storage_relpath(new_path)
        doc.canonical_filename = new_name
        await session.commit()

    return _to_out(doc)


@router.post("/{document_id}/reprocess", response_model=DocumentOut)
async def reprocess_document(document_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> DocumentOut:
    """Re-run OCR -> extraction -> chunk/embed against the file already on
    disk (no re-upload). Useful after a prompt/model change, or to retry a
    document that came back mis-extracted. Safe to call on any status except
    one already mid-run."""
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    if doc.status == STATUS_PROCESSING:
        raise HTTPException(409, "document is already being processed")

    path = absolute_path(doc.stored_path)
    if not path.exists():
        raise HTTPException(410, "file missing from storage")

    doc.status = STATUS_QUEUED
    doc.error = None
    await session.commit()

    job = Job(document_id=doc.id, kind="reprocess", status=STATUS_QUEUED)
    session.add(job)
    await session.commit()

    await enqueue_process_document(str(doc.id), str(job.id))
    return _to_out(doc)


@router.get("/{document_id}/download")
async def download_document(document_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> FileResponse:
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    path = absolute_path(doc.stored_path)
    if not path.exists():
        raise HTTPException(410, "file missing from storage")
    filename = doc.canonical_filename or doc.original_filename
    return FileResponse(path, filename=filename, media_type=doc.mime_type)


@router.delete("/{document_id}", status_code=204, response_model=None)
async def delete_document(document_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> None:
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    path = absolute_path(doc.stored_path)
    await session.delete(doc)
    await session.commit()
    if path.exists():
        path.unlink(missing_ok=True)
