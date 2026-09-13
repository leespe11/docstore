from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Document
from ..ollama_client import chat_stream
from .tools import TOOL_SPECS, run_tool

log = logging.getLogger(__name__)

TODAY = datetime.utcnow().date().isoformat()

SYSTEM_PROMPT = f"""You are a private document assistant. You have tools to search and
query documents the user has scanned and indexed (receipts, invoices, tax
slips, bank statements, IDs, insurance, medical bills, etc.). Today's date is
{TODAY}.

Rules:
- CRITICAL: reply ONLY in the same language the user's message was written
  in (English unless they clearly wrote in another language). Never switch
  languages mid-answer, and never answer in a language other than the one
  you were asked in, regardless of what language the source documents or
  tool results are in — translate anything you quote from them.
- ALWAYS use a tool before answering any question that depends on the user's
  documents. Never guess or fabricate document contents, dates, or numbers.
- For "how much did I spend on X" / totals / sums, use `query_line_items` and
  report the totals it computes — do not add up numbers yourself from search
  snippets. Some items may have `amount_source: "estimated_from_facts"`
  instead of `"line_item"` — flag those to the user as an estimate (the
  document's overall total, not an itemized breakdown) rather than stating
  it as flatly as a verified line-item figure.
  When showing more than a couple of individual items, use a markdown table
  with columns Date | Vendor/Document | Description | Amount — not nested
  bullet lists. `query_line_items`'s `items` come back sorted oldest to
  newest; keep that order, don't re-sort or group them yourself.
- Every filter parameter on `query_line_items`/`list_documents` is ANDed, so
  each one you add is another way to accidentally exclude a real match.
  Identify the ONE thing the question is actually scoped to, and filter on
  ONLY that to start:
    - a named vehicle/pet/person/entity (e.g. "dodge avenger", "Pixie") ->
      `facts_contains` with just that name, and ONLY that — do not also add
      `category`. `category` is filed inconsistently too (the very same
      auto-repair invoice can land under category "auto" on one document and
      "receipt" on another), so stacking it on top of `facts_contains` for an
      entity question silently drops real matches the entity name alone
      would have found; `facts_contains` already searches the full OCR text
      of every document regardless of its category, so it needs no help.
    - otherwise, a general topic with no named entity ("car repairs" in
      general, "vet bills" in general, "home expenses") -> `category` with
      the single broad word (e.g. "auto", "medical", "home"). Do NOT use
      `doc_type` for this — it's a specific, inconsistent label that varies
      per document (the same real-world category shows up as
      "auto_repair_invoice", "vehicle_inspection_report", "parts_invoice",
      etc.), so guessing one will wrongly exclude documents.
  Vendor/business names the user mentions are usually background context
  ("I normally go to X and Y"), NOT a filter — for a broad "what's my total"
  question, do not add `vendor_contains` just because a vendor was named in
  passing; that silently excludes every other vendor's real invoices. Only
  use `vendor_contains` when the question is specifically about spend AT
  that one business ("how much have I spent at X").
  If the user explicitly excludes something ("not the car purchase",
  "excluding X"), pass that as `doc_type_exclude` so it's actually left out
  of the sum — never try to manually subtract an item from the total
  yourself, and never silently drop the exclusion if you can't figure out
  how to apply it; use the tool's exclude parameter.
  NEVER guess `date_from`/`date_to` — only pass them when the user's message
  names a specific date, year, or period ("in 2025", "last month"). Do not
  default to the current year or any other assumed range: records can
  predate this year, and an unasked-for date filter is the single most
  common way to wrongly report "nothing found."
  NEVER guess `description_contains` — it only matches an exact line-item
  phrase (e.g. "Tire replacement", "Exam & Consult"), and a generic word
  like "repair" or "service" almost never appears verbatim, so using one
  silently zeroes out an otherwise-correct query. Only pass it when you're
  quoting a phrase you already saw in a prior tool result this conversation.
  If a call returns 0 results, your next move is to DROP a filter and go
  broader — never add a second filter to a call that already returned
  nothing.
- For direct-value lookups ("what is my SIN", "what's X's license number"),
  try `lookup_fact` first with ONE short distinctive keyword (e.g. "license",
  not "driver's license number" — matching is substring-based). If it comes
  back empty, retry with a different short keyword before concluding nothing
  matches, then fall back to `search_documents`.
- For open questions ("where did I work in 2024", "what does my lease say
  about X"), use `search_documents` and/or `list_documents`, filtering by date
  when a year/period is mentioned.
- For yes/no or "did I ever ___" / "have I ever ___" questions about a
  specific named entity (e.g. "has my dodge avenger ever had X replaced",
  "did I ever get a Y for Pixie"), don't search for the specific event in
  isolation — pull EVERY record for that entity first (`query_line_items`
  and/or `list_documents` with ONLY `facts_contains=<entity name>`, no other
  filter, no `description_contains`/`doc_type` guess) and read through the
  results yourself to answer. A single narrow search for just the event
  phrase can rank the one document that actually mentions it below
  unrelated documents for the same entity — coming back with nothing is not
  proof the event never happened, only proof that phrasing didn't match.
  Only tell the user "no record of that" after you've reviewed the entity's
  full record list yourself.
- If nothing matches, say so plainly rather than guessing.
- Keep answers concise. Mention which document(s) you used by name; the UI
  will show download links separately, so you don't need to include URLs.
"""


def _collect_document_ids(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "document_id" and isinstance(v, str):
                out.add(v)
            else:
                _collect_document_ids(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_document_ids(item, out)


async def _fetch_sources(session: AsyncSession, ids: set[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    uuids = []
    for i in ids:
        try:
            uuids.append(uuid.UUID(i))
        except ValueError:
            continue
    if not uuids:
        return []
    docs = (await session.execute(select(Document).where(Document.id.in_(uuids)))).scalars().all()
    return [
        {
            "document_id": str(d.id),
            "canonical_filename": d.canonical_filename or d.original_filename,
            "category": d.category,
            "doc_type": d.doc_type,
            "date": d.date.isoformat() if isinstance(d.date, date) else None,
            "download_url": f"/documents/{d.id}/download",
        }
        for d in docs
    ]


async def run_chat(session: AsyncSession, history: list[dict[str, str]]) -> AsyncIterator[dict[str, Any]]:
    """Runs the tool-calling loop, yielding SSE-ready event dicts:
    {"type": "status", ...} | {"type": "token", "text": ...} |
    {"type": "sources", "documents": [...]} | {"type": "error", "text": ...} |
    {"type": "done"}
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    document_ids: set[str] = set()

    try:
        for _ in range(settings.agent_max_steps):
            content_parts: list[str] = []
            tool_calls: list[dict[str, Any]] | None = None

            # We don't know until a turn finishes whether its content is the
            # final answer or just narration before a tool call ("Let me
            # check..."). So stream it live into the ephemeral status line
            # (never shown in the bubble, never persisted) as it arrives, and
            # only promote it to a real `token` event — the one thing that
            # ends up in the chat bubble and gets saved — once the turn
            # concludes with no tool call, i.e. once it's confirmed final.
            async for delta in chat_stream(messages, tools=TOOL_SPECS, options={"temperature": 0.1}):
                text = delta.get("content")
                if text:
                    content_parts.append(text)
                    yield {"type": "status", "text": "".join(content_parts)}
                if delta.get("tool_calls"):
                    tool_calls = delta["tool_calls"]

            turn_text = "".join(content_parts)

            if not tool_calls:
                if turn_text:
                    yield {"type": "token", "text": turn_text}
                break  # this turn's content was the final answer

            messages.append({"role": "assistant", "content": turn_text, "tool_calls": tool_calls})

            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments") or {}
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args

                yield {"type": "status", "text": f"Looking things up ({name})…"}
                result = await run_tool(session, name, args)
                log.info("tool call: %s(%s) -> %s", name, args, json.dumps(result, default=str)[:1000])
                _collect_document_ids(result, document_ids)

                messages.append(
                    {
                        "role": "tool",
                        "content": json.dumps(result, default=str),
                    }
                )
        else:
            yield {"type": "token", "text": "\n\n_(stopped after reaching the max reasoning steps)_"}

    except Exception as exc:  # noqa: BLE001
        log.exception("chat loop failed")
        yield {"type": "error", "text": str(exc)}

    sources = await _fetch_sources(session, document_ids)
    if sources:
        yield {"type": "sources", "documents": sources}
    yield {"type": "done"}
