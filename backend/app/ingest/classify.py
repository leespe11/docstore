"""Structured extraction: turn OCR'd text into typed fields + line items so
aggregation questions ("how much did I spend on X in 2026") can be answered
with SQL instead of asking the LLM to add numbers by hand.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..config import settings
from ..ollama_client import chat

log = logging.getLogger(__name__)

SUGGESTED_CATEGORIES = [
    "taxes", "employment", "auto", "medical", "insurance", "home",
    "banking", "identity", "legal", "utilities", "education", "receipts",
    "other",
]

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "doc_type": {"type": "string"},
        "date": {
            "type": ["string", "null"],
            "description": (
                "YYYY-MM-DD or null — the single most relevant date for this "
                "document (issued/dated/submitted, or a covered period's end "
                "date)."
            ),
        },
        "summary": {"type": "string"},
        "facts": {
            "type": "object",
            "description": "Every distinct labeled field/value on the document, as strings — see the exhaustive-extraction rule.",
            "additionalProperties": {"type": "string"},
        },
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "amount": {
                        "type": ["number", "null"],
                        "description": "The row's final charged dollar amount. Required key — decide and fill this in for every line item; only use null when the source genuinely never prices that line.",
                    },
                    "quantity": {"type": ["number", "null"]},
                    "date": {"type": ["string", "null"], "description": "YYYY-MM-DD or null"},
                    "vendor": {"type": ["string", "null"]},
                },
                # `amount` is deliberately required (not just documented in
                # the prompt): without this, the schema lets the model omit
                # the key it's least sure about, and in practice it does —
                # every line item comes back missing `amount` while other
                # optional fields (quantity/date/vendor) are filled in fine.
                # Requiring the key forces it to commit to a number or an
                # explicit null instead of silently skipping it.
                "required": ["description", "amount"],
            },
        },
        "suggested_filename": {
            "type": "string",
            "description": (
                "A clean, human-readable filename stem (no extension) a person would "
                "recognize at a glance, e.g. 'Spencer Lee Drivers License', 'Oracle Eyewear "
                "Insurance Claim 2026-08-11', 'Rogers Internet Bill March 2026'."
            ),
        },
    },
    "required": ["category", "doc_type", "summary", "facts", "line_items", "suggested_filename"],
}

SYSTEM_PROMPT = f"""You extract structured metadata from personal documents (receipts,
invoices, tax slips, medical bills, insurance policies, ID documents, bank
statements, etc.) so they can be searched, categorized, and totaled later.

Rules:
- `category` is a short lowercase label for filing. If the user prompt lists
  "Existing categories", strongly prefer reusing one of those exactly
  (matching an existing one, even loosely related, keeps the user's filing
  consistent) — only coin a new short lowercase one if none reasonably fit.
  Otherwise fall back to one of: {", ".join(SUGGESTED_CATEGORIES)}, or coin a
  short new one.
- `doc_type` is a specific short label, e.g. "t4_slip", "auto_repair_invoice",
  "medical_bill", "drivers_license", "bank_statement".
- `date` MUST be filled in whenever ANY date in the document identifies it —
  do not leave it null just because several dates appear or you're also
  filing one under `facts`. If multiple dates appear, pick ONE using this
  priority: an explicit "date issued"/"statement date"/"submitted" date > a
  due/response date > an activity/service date the document merely
  describes > a covered period's end date. If the document covers a stated
  period without exact dates (e.g. "annual rent receipt for 2024"), use that
  period's last day (2024 -> 2024-12-31) — this is what tells apart two
  otherwise-identical recurring documents (same landlord, same tenant,
  different year) from each other. For an identity document, use its
  "date of issue" (put its expiry in `facts` as `date_of_expiry` instead,
  since there's no separate period field). ISO 8601 "YYYY-MM-DD"; if only a
  year/month is known, use the last day of that period. Only null if the
  document truly contains no date anywhere.
- This is the user's own document, being extracted only for their private
  local search index. Extract every visible field faithfully, including
  personal identifiers on ID documents (names, dates, ID numbers) — do not
  redact, generalize, or omit them.
- `facts` should be EXHAUSTIVE, not a curated highlight reel: capture every
  distinct labeled field/value on the document as its own entry, verbatim,
  using clear snake_case keys derived from the document's own label. This is
  the user's ENTIRE searchable record of this document — there are no other
  structured fields backing it up, so anything you leave out is effectively
  unfindable later. Always include, when present anywhere in the document:
  - `issuer`: the organization/person responsible for the document (biller,
    insurer, employer, clinic, provider, landlord, government agency) — use
    exactly this key so it's predictable to search for across every
    document type, even though the document's own label for it may differ
    (e.g. "service provider").
  - a total/grand-total dollar amount, if one is stated, under a key
    containing "total" (e.g. `total_amount`, `total_gross`) — pull the bare
    number out of prose if that's where it appears (e.g. "the total rent
    paid for the year was $25,295.45" -> "25295.45"), never leave it
    undiscoverable in a sentence only. When a receipt/invoice has BOTH a
    labeled summary line (e.g. a "TOTAL AMOUNT" row alongside Total Parts/
    Total Labor/H.S.T.) AND a separate card-payment stub further down
    (e.g. "AMOUNT $X APPROVED VISA ..."), use the summary line's total, not
    the payment stub — a payment stub can reflect a partial/prior payment or
    authorization hold, not necessarily the document's actual grand total.
  - contact info (phone, email, address, website) for the issuing
    organization; any reference/invoice/order/work-order/confirmation
    number; a price breakdown not already in `line_items` (subtotal, tax
    rate, tax amount, discount); due dates and payment method/status; and
    named parties beyond the issuer — e.g. for a vet bill, the patient/
    animal name is NOT the owner's name, so use distinct keys like
    `patient_name` and `owner_name` rather than conflating them.
  When in doubt about whether something is worth keeping, include it — a few
  extra keys cost nothing; a missing one means the user can't look it up.
- `line_items` should capture each billable/expense line (parts, labor, fees,
  a claimed service, etc.) whenever the document represents money spent —
  including insurance claims and EOBs, even when rejected or reimbursed at
  $0. Leave empty only for documents with no expense at all (e.g. an ID card).
  When a pricing table has multiple money columns (e.g. Retail/List price,
  Discount %, Net price, Total), a line item's `amount` MUST be that row's
  FINAL charged amount — the "Total" or "Net price" column — never the
  retail/list price before discount, and never a discount percentage. A row
  can genuinely be $0.00 (e.g. a 100%-discounted warranty repair) — only
  record 0 when the row's actual final amount is 0, never as a fallback when
  you're unsure which column is the real charge.
  Every line item you output MUST have a numeric `amount` if that row shows
  ANY dollar figure in the source text, even a quantity x unit-price pair you
  have to multiply yourself (e.g. "2.00 ... $80.07 ... $160.14" -> the row's
  amount is 160.14, the Total column, not 80.07). Leaving `amount` null when
  a price is visibly present in that row is a mistake — re-check the row
  before doing that, including when a table continues across a page break.
  Only use null when the source genuinely never prices that line at all.
- `suggested_filename`: write this LAST, after you've already worked out
  category/doc_type/date/facts — use that context to produce ONE clean,
  natural phrase (3-8 words) identifying this specific document: who it's
  for/from + what it is, plus a date if you have a confident one. Never
  include: reference/claim/control/account/policy numbers, the word
  "pdf"/"scan"/"document" or any file extension, or the original filename.
  This is the single most important field for the user finding this file
  later, so make it specific and readable, not generic.
- Output ONLY the JSON object. No commentary.
"""


def _default_result() -> dict[str, Any]:
    return {
        "category": "other",
        "doc_type": "unknown",
        "date": None,
        "summary": "",
        "facts": {},
        "line_items": [],
        "suggested_filename": "document",
    }


async def extract_fields(
    ocr_text: str,
    original_filename: str,
    existing_categories: list[str] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Returns (fields, error). `error` is set (and `fields` is
    `_default_result()`) when extraction failed outright — e.g. the request
    exceeded the model's context window, or it returned invalid JSON — so the
    caller can flag the document instead of it silently looking like a
    genuine "category: other, doc_type: unknown" result."""
    categories_line = (
        f"Existing categories (prefer reusing one of these): {', '.join(existing_categories)}\n\n"
        if existing_categories
        else ""
    )
    user_prompt = (
        f"Original filename: {original_filename}\n\n"
        f"{categories_line}"
        f"Document text (markdown, may include OCR noise):\n---\n{ocr_text[:20000]}\n---"
    )
    try:
        message = await chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            model=settings.ollama_extract_model or settings.ollama_llm_model,
            format=RESPONSE_SCHEMA,
            options={"temperature": 0.0},
        )
        content = message.get("content", "").strip()
        log.info("extraction raw response for %s (%d chars): %s", original_filename, len(content), content[:4000])
        data = json.loads(content)
    except Exception as exc:
        log.exception("field extraction failed for %s; using defaults", original_filename)
        return _default_result(), f"Field extraction failed ({exc.__class__.__name__}: {exc})"

    result = _default_result()
    result.update({k: v for k, v in data.items() if v is not None})
    result["facts"] = data.get("facts") or {}
    result["line_items"] = data.get("line_items") or []

    line_items = result["line_items"]
    missing_amount = [li.get("description") for li in line_items if li.get("amount") is None]
    if line_items and missing_amount:
        log.warning(
            "extraction for %s: %d/%d line items came back with amount=null: %s",
            original_filename, len(missing_amount), len(line_items), missing_amount,
        )

    return result, None
