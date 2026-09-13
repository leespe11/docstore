"""Tools the chat agent can call against the indexed documents."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import Text, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Chunk, Document, LineItem, STATUS_INDEXED
from ..moneyfacts import amount_from_facts as _amount_from_facts
from ..ollama_client import embed


def _facts_contains_clause(term: str):
    """`facts_contains` is meant to find "does this document mention entity
    X" (a vehicle, pet, person...), but the extraction model is inconsistent
    about which details it promotes into the structured `facts` JSON — e.g.
    a vehicle's VIN/plate got captured but its make/model didn't. Matching
    against `facts` alone silently misses real documents. `ocr_text` is the
    one field guaranteed to contain everything actually on the page
    regardless of what the model chose to structure, so fall back to it
    (and `summary`, which is prose and often names the entity too)."""
    like = f"%{term}%"
    return or_(
        cast(Document.facts, Text).ilike(like),
        Document.ocr_text.ilike(like),
        Document.summary.ilike(like),
    )


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": (
                "Semantic search over the text of indexed documents. Use for open-ended "
                "questions ('what does my lease say about parking', 'find my passport "
                "renewal letter'). Not for totals/sums — use query_line_items for that."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "natural-language search query"},
                    "category": {"type": "string", "description": "optional category filter"},
                    "doc_type": {"type": "string", "description": "optional doc type filter"},
                    "date_from": {"type": "string", "description": "optional ISO date lower bound"},
                    "date_to": {"type": "string", "description": "optional ISO date upper bound"},
                    "limit": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_line_items",
            "description": (
                "Sum and list expense/line-item records (receipts, invoices, bills). Use "
                "this for any 'how much did I spend on X' / 'total cost of Y' question. "
                "Filters are ANDed and all optional; omit ones you don't need. If the "
                "question names a specific entity that ISN'T a vendor or line description — "
                "a pet's name, a specific person, a license plate, a VIN — use "
                "`facts_contains` instead of vendor_contains/description_contains, since "
                "those details live in the document's extracted facts, not the line item text. "
                "Each returned item has `amount_source`: 'line_item' (a real extracted amount — "
                "trust it fully) or 'estimated_from_facts' (that document had no usable per-line "
                "breakdown, so this is its largest facts figure that looks like a total — mention "
                "to the user that this specific number is estimated, not itemized). If the user "
                "says 'not X' / 'excluding X' (e.g. 'not the car purchase'), pass X as "
                "`doc_type_exclude` so it's actually left out of the sum — never try to manually "
                "subtract an item's amount from the reported total yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "doc_type": {"type": "string"},
                    "doc_type_exclude": {
                        "type": "string",
                        "description": "Excludes documents whose doc_type matches this (e.g. 'purchase' to drop a purchase agreement from a repairs total).",
                    },
                    "vendor_contains": {"type": "string"},
                    "description_contains": {"type": "string"},
                    "facts_contains": {
                        "type": "string",
                        "description": "Matches this against the owning document's extracted facts (any key/value) — e.g. a pet or person's name.",
                    },
                    "date_from": {"type": "string", "description": "ISO date lower bound"},
                    "date_to": {"type": "string", "description": "ISO date upper bound"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_fact",
            "description": (
                "Find a specific extracted fact across all documents by key, e.g. 'sin', "
                "'employer', 'policy_number', 'account_number', 'vin', 'license'. Use for "
                "direct lookups like 'what is my SIN number' or 'what's X's license number'. "
                "Matching ignores case/spacing/punctuation but is still substring-based — "
                "prefer ONE short distinctive keyword ('license', 'account') over a full "
                "phrase ('driver's license number'), and try a couple of keywords before "
                "concluding nothing matches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key_contains": {"type": "string", "description": "short keyword to search for within fact keys"},
                },
                "required": ["key_contains"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": (
                "List indexed documents by category/type/date range, most recent first. "
                "For a specific named entity (a pet's name, a person, a license plate) use "
                "`facts_contains`, not category/doc_type."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "doc_type": {"type": "string"},
                    "doc_type_exclude": {
                        "type": "string",
                        "description": "Excludes documents whose doc_type matches this — use for 'not X'/'excluding X' in the question.",
                    },
                    "facts_contains": {
                        "type": "string",
                        "description": "Matches this against a document's extracted facts (any key/value).",
                    },
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_document",
            "description": "Get full metadata, summary, and extracted facts for one document by id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                },
                "required": ["document_id"],
            },
        },
    },
]


def _parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


async def search_documents(
    session: AsyncSession,
    *,
    query: str,
    category: str | None = None,
    doc_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    vectors = await embed([query])
    qvec = vectors[0]
    distance = Chunk.embedding.cosine_distance(qvec).label("distance")

    stmt = (
        select(
            Chunk.content,
            Document.id,
            Document.canonical_filename,
            Document.category,
            Document.doc_type,
            Document.date,
            distance,
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(Document.status == STATUS_INDEXED)
    )
    if category:
        stmt = stmt.where(Document.category.ilike(f"%{category}%"))
    if doc_type:
        stmt = stmt.where(Document.doc_type.ilike(f"%{doc_type}%"))
    d_from, d_to = _parse_date(date_from), _parse_date(date_to)
    if d_from:
        stmt = stmt.where(Document.date >= d_from)
    if d_to:
        stmt = stmt.where(Document.date <= d_to)

    stmt = stmt.order_by(distance).limit(min(limit or 8, 25))
    rows = (await session.execute(stmt)).all()

    return {
        "results": [
            {
                "document_id": str(r.id),
                "canonical_filename": r.canonical_filename,
                "category": r.category,
                "doc_type": r.doc_type,
                "date": r.date.isoformat() if r.date else None,
                "snippet": r.content[:800],
                "relevance": round(1 - float(r.distance), 4),
            }
            for r in rows
        ]
    }


async def query_line_items(
    session: AsyncSession,
    *,
    category: str | None = None,
    doc_type: str | None = None,
    doc_type_exclude: str | None = None,
    vendor_contains: str | None = None,
    description_contains: str | None = None,
    facts_contains: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    async def fetch(desc_filter: str | None, type_filter: str | None) -> list[Any]:
        stmt = (
            select(LineItem, Document.canonical_filename, Document.doc_type, Document.facts)
            .join(Document, Document.id == LineItem.document_id)
            .where(Document.status == STATUS_INDEXED)
        )
        if category:
            stmt = stmt.where(LineItem.category.ilike(f"%{category}%"))
        if type_filter:
            stmt = stmt.where(Document.doc_type.ilike(f"%{type_filter}%"))
        if doc_type_exclude:
            stmt = stmt.where(~Document.doc_type.ilike(f"%{doc_type_exclude}%"))
        if vendor_contains:
            stmt = stmt.where(LineItem.vendor.ilike(f"%{vendor_contains}%"))
        if desc_filter:
            stmt = stmt.where(LineItem.description.ilike(f"%{desc_filter}%"))
        if facts_contains:
            stmt = stmt.where(_facts_contains_clause(facts_contains))
        d_from, d_to = _parse_date(date_from), _parse_date(date_to)
        if d_from:
            stmt = stmt.where(LineItem.line_date >= d_from)
        if d_to:
            stmt = stmt.where(LineItem.line_date <= d_to)
        # Oldest-to-newest so both `items` and the by-document grouping below
        # come out chronological without the model having to re-sort anything
        # itself when it lists them — it's inconsistent at that.
        stmt = stmt.order_by(LineItem.line_date.asc().nullslast())
        return (await session.execute(stmt)).all()

    rows = await fetch(description_contains, doc_type)

    # The single most common way this silently reports "nothing found" for a
    # real record: the model pairs a reliable entity filter (facts_contains)
    # with a *guessed* fragile one (description_contains only matches an
    # exact line-item phrase; doc_type is an inconsistent per-document label)
    # — despite the system prompt telling it not to. If that combo comes back
    # empty, retry with just the entity filter before conceding nothing
    # matches, rather than trusting the model to always follow that rule.
    broadened = False
    if facts_contains and not rows and (description_contains or doc_type):
        rows = await fetch(None, None)
        broadened = True

    # Group by document: if NONE of a document's matched line items have a
    # real extracted amount, estimate that document's total from its facts
    # on the fly instead of silently reporting $0 — nothing is written to
    # the DB, this is recomputed fresh on every call.
    by_doc: dict[str, dict[str, Any]] = {}
    for li, canonical_filename, doc_type_val, facts in rows:
        doc_id = str(li.document_id)
        entry = by_doc.setdefault(
            doc_id,
            {"canonical_filename": canonical_filename, "doc_type": doc_type_val, "facts": facts, "line_items": []},
        )
        entry["line_items"].append(li)

    items: list[dict[str, Any]] = []
    totals_by_currency: dict[str, float] = {}

    for doc_id, entry in by_doc.items():
        lis = entry["line_items"]
        has_real_amount = any(li.amount is not None for li in lis)

        for li in lis:
            amount = float(li.amount) if li.amount is not None else None
            if amount is not None:
                currency = li.currency or "UNKNOWN"
                totals_by_currency[currency] = totals_by_currency.get(currency, 0.0) + amount
            items.append(
                {
                    "document_id": doc_id,
                    "canonical_filename": entry["canonical_filename"],
                    "doc_type": entry["doc_type"],
                    "description": li.description,
                    "amount": amount,
                    "amount_source": "line_item" if amount is not None else "unavailable",
                    "currency": li.currency,
                    "date": li.line_date.isoformat() if li.line_date else None,
                    "vendor": li.vendor,
                }
            )

        if not has_real_amount:
            estimated = _amount_from_facts(entry["facts"])
            if estimated is not None:
                totals_by_currency["UNKNOWN"] = totals_by_currency.get("UNKNOWN", 0.0) + estimated
                items.append(
                    {
                        "document_id": doc_id,
                        "canonical_filename": entry["canonical_filename"],
                        "doc_type": entry["doc_type"],
                        "description": "(estimated document total — per-line amounts unavailable)",
                        "amount": estimated,
                        "amount_source": "estimated_from_facts",
                        "currency": None,
                        "date": None,
                        "vendor": None,
                    }
                )

    result: dict[str, Any] = {
        "count": len(items),
        "totals_by_currency": totals_by_currency,
        "items": items,
    }
    if broadened:
        result["note"] = (
            "description_contains/doc_type combined with facts_contains matched nothing, "
            "so results were broadened to facts_contains alone — review the items below "
            "yourself for the one the user asked about rather than assuming none apply."
        )
    return result


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize_key(s: str) -> str:
    """'driver's license number' / 'Driver License Number' / 'driver_license_number'
    all normalize to the same string, so lookup_fact isn't defeated by the model
    phrasing a query differently than our snake_case key convention."""
    return _NON_ALNUM.sub("", s.lower())


async def lookup_fact(session: AsyncSession, *, key_contains: str) -> dict[str, Any]:
    # `facts` is a small jsonb blob per document; filtering in Python is simplest and
    # avoids a set-returning function in the query.
    doc_stmt = select(Document.id, Document.canonical_filename, Document.facts).where(
        Document.status == STATUS_INDEXED, Document.facts != {}
    )
    rows = (await session.execute(doc_stmt)).all()

    matches = []
    needle = _normalize_key(key_contains)
    for doc_id, canonical_filename, facts in rows:
        for k, v in (facts or {}).items():
            if needle and needle in _normalize_key(k):
                matches.append(
                    {
                        "document_id": str(doc_id),
                        "canonical_filename": canonical_filename,
                        "key": k,
                        "value": v,
                    }
                )
    return {"matches": matches}


async def list_documents(
    session: AsyncSession,
    *,
    category: str | None = None,
    doc_type: str | None = None,
    doc_type_exclude: str | None = None,
    facts_contains: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    async def fetch(type_filter: str | None) -> list[Any]:
        stmt = select(Document).where(Document.status == STATUS_INDEXED)
        if category:
            stmt = stmt.where(Document.category.ilike(f"%{category}%"))
        if type_filter:
            stmt = stmt.where(Document.doc_type.ilike(f"%{type_filter}%"))
        if doc_type_exclude:
            stmt = stmt.where(~Document.doc_type.ilike(f"%{doc_type_exclude}%"))
        if facts_contains:
            stmt = stmt.where(_facts_contains_clause(facts_contains))
        d_from, d_to = _parse_date(date_from), _parse_date(date_to)
        if d_from:
            stmt = stmt.where(Document.date >= d_from)
        if d_to:
            stmt = stmt.where(Document.date <= d_to)
        stmt = stmt.order_by(Document.date.desc().nullslast()).limit(min(limit or 20, 100))
        return (await session.execute(stmt)).scalars().all()

    docs = await fetch(doc_type)

    # Same rationale as query_line_items: facts_contains (reliable) paired
    # with a guessed doc_type (inconsistent per-document label) is a common
    # way to silently miss a real document — broaden before conceding.
    broadened = False
    if facts_contains and not docs and doc_type:
        docs = await fetch(None)
        broadened = True

    result: dict[str, Any] = {
        "documents": [
            {
                "document_id": str(d.id),
                "canonical_filename": d.canonical_filename,
                "category": d.category,
                "doc_type": d.doc_type,
                "date": d.date.isoformat() if d.date else None,
                "summary": d.summary,
            }
            for d in docs
        ]
    }
    if broadened:
        result["note"] = (
            "doc_type combined with facts_contains matched nothing, so results were "
            "broadened to facts_contains alone — review the documents below yourself for "
            "the one the user asked about rather than assuming none apply."
        )
    return result


async def get_document(session: AsyncSession, *, document_id: str) -> dict[str, Any]:
    import uuid

    try:
        doc = await session.get(Document, uuid.UUID(document_id))
    except ValueError:
        doc = None
    if doc is None:
        return {"error": f"no document with id {document_id}"}

    return {
        "document_id": str(doc.id),
        "canonical_filename": doc.canonical_filename,
        "original_filename": doc.original_filename,
        "category": doc.category,
        "doc_type": doc.doc_type,
        "date": doc.date.isoformat() if doc.date else None,
        "summary": doc.summary,
        "facts": doc.facts,
        "status": doc.status,
        "text_excerpt": (doc.ocr_text or "")[:4000],
    }


DISPATCH = {
    "search_documents": search_documents,
    "query_line_items": query_line_items,
    "lookup_fact": lookup_fact,
    "list_documents": list_documents,
    "get_document": get_document,
}


async def run_tool(session: AsyncSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    fn = DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool '{name}'"}
    try:
        return await fn(session, **arguments)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{name} failed: {exc}"}
