"""Shared heuristic for estimating a document's total from its `facts` blob
when structured line-item amounts aren't available or aren't trusted. Used by
dedupe scoring, query-time total estimation, and ingest-time reconciliation
logging — kept in one place so all three agree on what counts as "the total".
"""

from __future__ import annotations

import re

# Currency-shaped only: optional $, optional thousands separators, EXACTLY
# two decimal digits. Invoice/phone/reference/HST-registration numbers
# almost never carry a decimal point, so this naturally excludes them
# without needing to guess at key names. The leading \d+ (not \d{1,3}) is
# deliberate: a 4+-digit total written WITHOUT a thousands separator (e.g.
# model output "2529.18" rather than "2,529.18", which is common) must still
# match — `\d{1,3}(?:,\d{3})*` alone rejects any ungrouped number over 3
# digits, silently dropping exactly the large totals this is meant to find.
MONEY_RE = re.compile(r"^-?\$?\d+(?:,\d{3})*\.\d{2}$")


def amount_from_facts(facts: dict | None) -> float | None:
    """Best-effort document total: the largest currency-shaped value found
    anywhere in `facts`, on the assumption the grand total is bigger than any
    subtotal, tax, or individual line amount. Never stored — computed fresh
    each time it's needed."""
    candidates: list[float] = []
    for value in (facts or {}).values():
        if not isinstance(value, str) or not MONEY_RE.match(value.strip()):
            continue
        try:
            candidates.append(float(value.strip().replace("$", "").replace(",", "")))
        except ValueError:
            continue
    return max(candidates) if candidates else None
