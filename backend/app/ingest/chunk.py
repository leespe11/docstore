from ..config import settings


def chunk_text(text: str) -> list[str]:
    """Paragraph-aware sliding-window chunking."""
    text = (text or "").strip()
    if not text:
        return []

    size = settings.chunk_chars
    overlap = settings.chunk_overlap
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            # try to break on a paragraph/sentence boundary near `end`
            boundary = text.rfind("\n\n", start, end)
            if boundary == -1 or boundary <= start + size // 2:
                boundary = text.rfind(". ", start, end)
            if boundary != -1 and boundary > start + size // 2:
                end = boundary + 1
        chunks.append(text[start:end].strip())
        if end >= n:
            break
        start = max(end - overlap, start + 1)

    return [c for c in chunks if c]


def facts_chunk(*, canonical_filename: str | None, summary: str | None, facts: dict) -> str | None:
    """A natural-language rendering of a document's extracted facts, embedded
    as an extra chunk alongside its OCR text — so a semantic search (not just
    the exact-key lookup_fact tool) can also surface things like a license or
    account number, which otherwise only exist in `facts`, never in prose."""
    if not facts:
        return None
    lines = [f"Document: {canonical_filename}"] if canonical_filename else []
    if summary:
        lines.append(f"Summary: {summary}")
    lines.append("Extracted details:")
    lines.extend(f"- {k.replace('_', ' ')}: {v}" for k, v in facts.items())
    return "\n".join(lines)
