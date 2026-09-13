from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime

from sqlalchemy import delete, select

from ..db import SessionLocal
from ..models import (
    STATUS_CANCELLED,
    STATUS_DUPLICATE,
    STATUS_ERROR,
    STATUS_INDEXED,
    STATUS_PROCESSING,
    Chunk,
    Document,
    Job,
    LineItem,
)
from ..moneyfacts import amount_from_facts
from ..ollama_client import embed
from ..storage import absolute_path, build_canonical_name, finalize_file, storage_relpath
from .chunk import chunk_text, facts_chunk
from .classify import extract_fields
from .dedupe import find_duplicate, grade as grade_document
from .ocr import ocr_document

log = logging.getLogger(__name__)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _clip(value, max_len: int) -> str | None:
    """Cap an LLM-generated string to a column's actual VARCHAR limit before
    it ever reaches an INSERT. Necessary defense: a small/weaker extraction
    model can (and did, in practice) dump a huge unrelated blob into a field
    like `vendor` — a raw DB length-constraint violation aborts the whole
    transaction, discarding OCR text/chunks/everything for that document
    over one bad field, not just that field."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_KEY_HINTS = ("date", "issue", "submitted", "statement", "invoice", "dated", "service", "expiry")
# The model names "who this document is from" inconsistently across
# documents (issuer, service_provider, invoice_location, ...) the same way
# it does for totals — broaden the fallback the same way, rather than
# depending on a single exact key.
_ISSUER_KEY_HINTS = ("issuer", "service_provider", "provider_name", "biller", "vendor_name", "location", "company")


def _date_from_facts(facts: dict) -> date | None:
    """`facts` reliably captures dates the model finds (a plain "date" key is
    common, plus e.g. an ID card's date_of_issue) even when it doesn't
    reliably promote one to the structured `date` column. Back that column
    with a deterministic scan instead of only hoping the prompt gets
    followed."""
    candidates = [(k, v) for k, v in facts.items() if isinstance(v, str) and _ISO_DATE_RE.match(v)]
    if not candidates:
        return None
    for hint in _DATE_KEY_HINTS:
        for key, value in candidates:
            if hint in key.lower():
                return _parse_date(value)
    return _parse_date(candidates[0][1])


def _issuer_from_facts(facts: dict) -> str | None:
    for hint in _ISSUER_KEY_HINTS:
        for key, value in facts.items():
            if hint in key.lower() and isinstance(value, str) and value.strip():
                return value.strip()
    return None


async def _existing_categories(session) -> list[str]:
    """Categories already in use (from prior ingests or manual edits), so new
    documents get filed consistently instead of the LLM coining near-duplicate
    labels each time."""
    rows = (
        await session.execute(select(Document.category).where(Document.category.isnot(None)).distinct())
    ).scalars().all()
    return sorted({c for c in rows if c})


async def _set_job(session, job_id: str | None, *, status: str, detail: str | None = None, error: str | None = None) -> None:
    if not job_id:
        return
    job = await session.get(Job, uuid.UUID(job_id))
    if job is None:
        return
    job.status = status
    if detail is not None:
        job.detail = detail
    if error is not None:
        job.error = error
    if status == STATUS_PROCESSING:
        job.attempts += 1
    await session.commit()


async def process_document(ctx, document_id: str, job_id: str | None = None) -> None:
    """arq task: OCR -> extract -> file -> chunk/embed a single document."""
    async with SessionLocal() as session:
        doc = await session.get(Document, uuid.UUID(document_id))
        if doc is None:
            log.warning("process_document: document %s not found", document_id)
            return

        # The job was already enqueued in arq/Redis by the time a user
        # cancels it from the UI, so this function still gets invoked — the
        # cancel endpoint just flips the document/job status ahead of time.
        # Once a job is actually mid-run it's too late to interrupt safely
        # (OCR may already be streaming to Ollama), so this only catches
        # cancellation that happened while still queued.
        if job_id:
            job = await session.get(Job, uuid.UUID(job_id))
            if job is not None and job.status == STATUS_CANCELLED:
                log.info("process_document: document %s was cancelled before this job started", document_id)
                return

        doc.status = STATUS_PROCESSING
        # Clears any line items/chunks from a previous run (a fresh document
        # has none, so this is a no-op there) — the loops below INSERT new
        # rows rather than upsert, so reprocessing without this would leave
        # stale + fresh rows both present, duplicating expense totals and
        # polluting vector search with duplicate chunks.
        await session.execute(delete(LineItem).where(LineItem.document_id == doc.id))
        await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
        # Also clear any stale duplicate flag from a prior run so the dedupe
        # check below starts fresh rather than compounding old state.
        doc.duplicate_of_id = None
        doc.duplicate_similarity = None
        doc.duplicate_reviewed = False
        await session.commit()
        await _set_job(session, job_id, status=STATUS_PROCESSING, detail="OCR in progress")

        try:
            path = absolute_path(doc.stored_path)

            # 1. OCR
            ocr_text, page_count = await ocr_document(path, doc.mime_type)
            doc.ocr_text = ocr_text
            doc.page_count = page_count
            await session.commit()
            await _set_job(session, job_id, status=STATUS_PROCESSING, detail="Extracting fields")

            # 2. classify + structured extraction
            existing_categories = await _existing_categories(session)
            fields, extraction_error = await extract_fields(ocr_text, doc.original_filename, existing_categories)
            doc.category = _clip(fields["category"], 64) or "other"
            doc.doc_type = _clip(fields["doc_type"], 64) or "unknown"
            doc.summary = fields.get("summary")
            doc.facts = fields.get("facts") or {}
            doc.date = _parse_date(fields.get("date")) or _date_from_facts(doc.facts)

            # No dedicated issuer/currency columns anymore — best-effort
            # fallback for line items' vendor when the model didn't fill in
            # each line's own `vendor` directly.
            issuer = _clip(_issuer_from_facts(doc.facts), 256)
            currency = _clip(doc.facts.get("currency"), 8)

            line_items = fields.get("line_items") or []
            for li in line_items:
                session.add(
                    LineItem(
                        document_id=doc.id,
                        description=li.get("description") or "(unspecified)",
                        amount=li.get("amount"),
                        quantity=li.get("quantity"),
                        currency=currency,
                        line_date=_parse_date(li.get("date")) or doc.date,
                        category=doc.category,
                        vendor=_clip(li.get("vendor"), 256) or issuer,
                    )
                )
            # No synthetic "total" line item inserted when per-line amounts
            # come back empty/zero — that would mean writing a guessed
            # number into the DB as if it were real extracted data. Instead,
            # `query_line_items` (agent/tools.py) estimates a document's
            # total on the fly from its `facts`, at query time, only when
            # asked — nothing stored here reflects a guess.

            # Diagnostic only (nothing below is written to the DB): flag at
            # ingest time when the per-line amounts and the facts-derived
            # estimate disagree, so extraction problems (a model returning
            # amount=null for every row, or picking up the wrong "total" —
            # e.g. a card-receipt stub amount instead of the invoice's real
            # summary-table total) surface in the logs immediately instead of
            # only being noticed later via a wrong chat answer.
            line_item_sum = sum(li.get("amount") or 0 for li in line_items) or None
            facts_estimate = amount_from_facts(doc.facts)
            if line_item_sum is None and facts_estimate is None and line_items:
                log.warning(
                    "document %s (%s): %d line items extracted but none has an amount, "
                    "and no money-shaped value found in facts either — total is unrecoverable",
                    doc.id, doc.original_filename, len(line_items),
                )
            elif (
                line_item_sum is not None
                and facts_estimate is not None
                and abs(line_item_sum - facts_estimate) > max(1.0, 0.02 * facts_estimate)
            ):
                log.warning(
                    "document %s (%s): line-item sum $%.2f disagrees with facts-estimated total $%.2f",
                    doc.id, doc.original_filename, line_item_sum, facts_estimate,
                )

            # 2b. near-duplicate check against other indexed documents (a
            # different scan of the same physical document rarely hash-
            # matches, so this is separate from the upload-time sha256 dedup)
            doc.quality_score = grade_document(doc, line_item_count=len(line_items))

            dup_match = await find_duplicate(session, doc)
            if dup_match is not None:
                candidate, similarity = dup_match
                if candidate.quality_score is None:
                    # pre-existing row from before this feature shipped, or
                    # never compared before -- approximate without touching
                    # its line_items relationship (would lazy-load, which
                    # isn't safe here since `candidate` wasn't eager-loaded)
                    candidate.quality_score = grade_document(candidate, line_item_count=0)

                loser = doc if doc.quality_score <= candidate.quality_score else candidate
                winner = candidate if loser is doc else doc
                loser.status = STATUS_DUPLICATE
                loser.duplicate_of_id = winner.id
                loser.duplicate_similarity = round(similarity, 4)
                loser.duplicate_reviewed = False

            await _set_job(session, job_id, status=STATUS_PROCESSING, detail="Filing and embedding")

            # 3. rename + file into its category folder
            ext = path.suffix.lower()
            canonical = build_canonical_name(
                suggested_filename=fields.get("suggested_filename") or doc.doc_type or "document",
                doc_date=doc.date.isoformat() if doc.date else None,
                ext=ext,
            )
            new_path = finalize_file(path, doc.category, canonical)
            doc.stored_path = storage_relpath(new_path)
            doc.canonical_filename = new_path.name

            # 4. chunk + embed
            chunks = chunk_text(ocr_text)
            extra = facts_chunk(canonical_filename=doc.canonical_filename, summary=doc.summary, facts=doc.facts)
            if extra:
                chunks.append(extra)
            if chunks:
                vectors = await embed(chunks)
                for idx, (content, vector) in enumerate(zip(chunks, vectors)):
                    session.add(
                        Chunk(document_id=doc.id, chunk_index=idx, content=content, embedding=vector)
                    )

            # `doc.status` may already be STATUS_DUPLICATE from the check
            # above (this scan turned out to be the weaker of a pair) — only
            # the winner (or a doc with no match at all) becomes STATUS_INDEXED.
            flagged_duplicate = doc.status == STATUS_DUPLICATE
            if not flagged_duplicate:
                doc.status = STATUS_INDEXED
            # Still "indexed" (OCR/chunking/embedding all succeeded and it's
            # searchable) but flagged if structured extraction itself failed,
            # rather than silently looking like a genuine "other/unknown"
            # result — surfaces as the row's tooltip in the UI.
            doc.error = extraction_error
            await session.commit()
            # The job itself succeeded either way; STATUS_DUPLICATE describes
            # the resulting document, not a job failure.
            await _set_job(
                session,
                job_id,
                status=STATUS_INDEXED,
                detail="Flagged as a possible duplicate — see Documents" if flagged_duplicate else "Done",
            )

        except Exception as exc:  # noqa: BLE001
            log.exception("processing failed for document %s", document_id)
            await session.rollback()
            doc = await session.get(Document, uuid.UUID(document_id))
            if doc is not None:
                doc.status = STATUS_ERROR
                doc.error = str(exc)
                await session.commit()
            await _set_job(session, job_id, status=STATUS_ERROR, error=str(exc))
            raise
