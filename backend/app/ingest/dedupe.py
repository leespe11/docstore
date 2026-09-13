"""Near-duplicate detection.

Two scans of the same physical document (a different pass on the scanner, a
photo instead of a scan, higher/lower DPI, a slightly crooked page) almost
never hash-match — different compression, different pixels, different OCR
noise — so the exact-sha256 dedup done at upload time misses them entirely.

This compares a newly-extracted document's text/facts/amount against other
already-indexed documents in the same category to catch those, and grades
both copies so the better extraction can be kept automatically while the
weaker one is suppressed (not deleted) pending the user's review. See
routers/documents.py's /duplicates endpoints and pipeline.py's integration.
"""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import select

from ..moneyfacts import amount_from_facts as _amount_from_facts
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import STATUS_INDEXED, Document

_WORD_RE = re.compile(r"[a-z0-9]+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(s: object) -> str:
    return _NON_ALNUM.sub("", str(s).lower())


def _text_similarity(a: str | None, b: str | None) -> float:
    """Jaccard similarity over lowercase word tokens. Cheap (no embeddings/
    LLM call needed) and tolerant of the word-order/spacing noise two OCR
    passes over the same page tend to disagree on."""
    ta = set(_WORD_RE.findall((a or "").lower()))
    tb = set(_WORD_RE.findall((b or "").lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _facts_overlap(a: dict | None, b: dict | None) -> float:
    """Fraction of keys present in BOTH documents' facts whose values agree
    (normalized). Only scores shared keys, so it doesn't penalize one OCR
    pass simply noticing a field the other missed."""
    a, b = a or {}, b or {}
    shared = set(a) & set(b)
    if not shared:
        return 0.0
    matches = sum(1 for k in shared if _norm(a[k]) == _norm(b[k]))
    return matches / len(shared)


def _amount_similarity(a: float | None, b: float | None) -> float:
    if a is None or b is None:
        return 0.5  # neutral: missing on either side shouldn't count against a match
    return 1.0 if abs(a - b) < 0.01 else 0.0


# A confirmed amount/date mismatch overrides high text/facts overlap
# entirely, rather than just diluting a blended average. Recurring documents
# from the same relationship (rent receipts from the same landlord/tenant,
# annual statements from the same provider) share a template and most of
# their facts (address, names, contact info) every single time — the amount
# or date is often the ONLY thing that actually differs, so it needs veto
# power, not a 15% vote.
_MISMATCH_CAP = 0.3


def _score(new: Document, existing: Document) -> float:
    text_sim = _text_similarity(new.ocr_text, existing.ocr_text)
    facts_sim = _facts_overlap(new.facts, existing.facts)
    new_amount = _amount_from_facts(new.facts)
    existing_amount = _amount_from_facts(existing.facts)

    blended = 0.55 * text_sim + 0.30 * facts_sim

    if new_amount is not None and existing_amount is not None and abs(new_amount - existing_amount) >= 0.01:
        return min(_MISMATCH_CAP, blended)
    if new.date is not None and existing.date is not None and new.date != existing.date:
        return min(_MISMATCH_CAP, blended)

    amount_sim = _amount_similarity(new_amount, existing_amount)
    return blended + 0.15 * amount_sim


async def find_duplicate(session: AsyncSession, doc: Document) -> tuple[Document, float] | None:
    """The best-matching existing indexed document, if its similarity score
    clears the configured threshold. Candidates are prefiltered to the same
    category (doc_type is model-phrased text and varies more run-to-run, so
    it's too strict a gate) and, when the new doc has a date, to within
    `dedupe_date_window_days` of it (or no date at all)."""
    stmt = select(Document).where(
        Document.status == STATUS_INDEXED,
        Document.id != doc.id,
        Document.category == doc.category,
    )
    if doc.date:
        window = timedelta(days=settings.dedupe_date_window_days)
        stmt = stmt.where(Document.date.is_(None) | Document.date.between(doc.date - window, doc.date + window))
    candidates = (await session.execute(stmt)).scalars().all()

    best: tuple[Document, float] | None = None
    for candidate in candidates:
        score = _score(doc, candidate)
        if score >= settings.dedupe_similarity_threshold and (best is None or score > best[1]):
            best = (candidate, score)
    return best


def grade(doc: Document, *, line_item_count: int = 0) -> float:
    """Rough, explainable extraction-quality score — higher is better. Only
    used to auto-pick which of two likely-duplicate scans to keep, so it
    just needs to be directionally right, not perfect."""
    text_len = len(doc.ocr_text or "")
    score = 0.0
    score += min(text_len, 6000) / 6000 * 3.0  # richer OCR text
    score += min(len(doc.facts or {}), 10) * 0.3  # more extracted facts
    score += min(line_item_count, 10) * 0.3  # more captured line items
    score += 1.0 if doc.date else 0.0
    # "issuer" is just one of several names the model uses inconsistently
    # for this (service_provider, invoice_location, ...) — a loose check for
    # any org-like fact is good enough for a tiebreaking heuristic.
    score += 1.0 if any(k.lower() in ("issuer", "service_provider", "provider_name", "biller") for k in (doc.facts or {})) else 0.0
    score += 1.0 if _amount_from_facts(doc.facts) is not None else 0.0
    score += min(doc.size_bytes or 0, 20_000_000) / 20_000_000 * 0.5  # tiny tiebreaker
    return round(score, 2)
