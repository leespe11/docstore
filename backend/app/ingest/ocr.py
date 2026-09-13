"""OCR: turn a stored file into layout-preserving markdown text via the
vision model. PDFs are rasterized page-by-page with PyMuPDF and sent to the
vision model UNLESS a page already has a real embedded text layer (a
true-text PDF, not a scan) — those pages are extracted directly instead,
which is both faster and more accurate than round-tripping through an image
and a vision model (no transcription-error risk for text that's already
digital). Word (.docx) documents always have a real text layer, so they
never go through OCR at all. Images go straight to the vision model;
already-text files (.txt/.md/.csv) are read as-is.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import fitz  # PyMuPDF
import pymupdf4llm
from docx import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph
from PIL import Image

from ..config import settings
from ..ollama_client import vision_transcribe

OCR_PROMPT = (
    "You are an OCR engine. Transcribe ALL text visible in this image exactly, "
    "preserving reading order, tables, and line items. Output GitHub-flavored "
    "markdown: use a markdown table for any tabular/line-item data (columns, "
    "amounts, dates). For a pricing/invoice table with multiple money columns "
    "(e.g. quantity, retail/list price, discount %, net price, total), give "
    "every column its own header and its own value per row — never collapse, "
    "merge, or drop a numeric column, and preserve exact figures including "
    "$0.00 values and discount percentages exactly as shown. Do not "
    "summarize, translate, or omit anything, including small print, headers, "
    "footers, and stamped/handwritten text. If the image has no legible "
    "text, output exactly: [no text]."
)

IMAGE_MIME_PREFIXES = ("image/",)
PLAIN_TEXT_MIME = {"text/plain", "text/markdown", "text/csv"}
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# A page's extracted text below this length is treated as "no real text
# layer" (a scanned image, or a blank/graphics-only page) and goes through
# vision OCR instead. Deliberately low: a false negative here just means an
# extra (slower but still correct) OCR pass on a short-but-real-text page,
# while a false positive would mean silently skipping a page that actually
# needed OCR — biased toward the safe failure mode.
MIN_TEXT_LAYER_CHARS = 20


async def ocr_document(path: Path, mime_type: str) -> tuple[str, int | None]:
    """Returns (markdown_text, page_count)."""
    if mime_type == DOCX_MIME or path.suffix.lower() == ".docx":
        return _extract_docx(path), None
    if mime_type == "application/pdf" or path.suffix.lower() == ".pdf":
        return await _ocr_pdf(path)
    if mime_type.startswith(IMAGE_MIME_PREFIXES) or path.suffix.lower() in {
        ".png", ".jpg", ".jpeg", ".webp", ".tiff", ".bmp",
    }:
        text = await _ocr_image_bytes(path.read_bytes())
        return text, 1
    if mime_type in PLAIN_TEXT_MIME or path.suffix.lower() in {".txt", ".md", ".csv"}:
        return path.read_text(errors="replace"), None

    # Fallback: try treating it as a single image (best-effort).
    text = await _ocr_image_bytes(path.read_bytes())
    return text, 1


def _docx_table_to_markdown(table: Table) -> str:
    rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    header, *body_rows = rows
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines.extend("| " + " | ".join(r) + " |" for r in body_rows)
    return "\n".join(lines)


def _extract_docx(path: Path) -> str:
    """Word documents are never scanned images — always read the real text/
    table content directly, in document order (paragraphs and tables are
    separate flat lists on python-docx's Document, so this walks the
    underlying XML body instead to interleave them correctly)."""
    document = DocxDocument(path)
    parts: list[str] = []
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            text = Paragraph(child, document).text.strip()
            if text:
                parts.append(text)
        elif child.tag.endswith("}tbl"):
            md = _docx_table_to_markdown(Table(child, document))
            if md:
                parts.append(md)
    return "\n\n".join(parts)


def _prepare_for_vision(data: bytes) -> bytes:
    """Downscale to `ocr_max_dimension` and re-encode as JPEG. Keeps large
    scans/photos from producing an oversized request body (some setups 413 on
    that) and keeps the vision model's image-token usage predictable,
    regardless of whether the source was a hi-res PDF render or a raw phone
    photo upload."""
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")

    longest = max(img.size)
    if longest > settings.ocr_max_dimension:
        scale = settings.ocr_max_dimension / longest
        new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        img = img.resize(new_size, Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=settings.ocr_jpeg_quality)
    return buf.getvalue()


async def _ocr_image_bytes(data: bytes) -> str:
    prepared = _prepare_for_vision(data)
    b64 = base64.b64encode(prepared).decode("ascii")
    return await vision_transcribe(b64, OCR_PROMPT)


def _page_zoom(page: "fitz.Page") -> float:
    """Zoom for settings.ocr_dpi, capped so an oddly-authored PDF can't blow
    up the render. Some scan-to-PDF tools set the page's MediaBox in pixel
    units at the scan DPI instead of standard 72-points-per-inch, which
    makes PyMuPDF see a page that's physically enormous (e.g. a 600 DPI
    8.5x11 scan reported as an ~85x110 inch page) — naively multiplying that
    by our own DPI zoom produces a hundreds-of-megapixel render. Cap the
    render itself at ocr_max_dimension; _prepare_for_vision would downscale
    to that anyway, so there's no quality lost, just wasted memory/CPU (or an
    outright crash) avoided."""
    rect = page.rect
    longest_pt = max(rect.width, rect.height) or 1.0
    dpi_zoom = settings.ocr_dpi / 72.0
    cap_zoom = settings.ocr_max_dimension / longest_pt
    return min(dpi_zoom, cap_zoom)


async def _vision_ocr_page(page: "fitz.Page") -> str:
    zoom = _page_zoom(page)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    prepared = _prepare_for_vision(pix.tobytes("png"))
    b64 = base64.b64encode(prepared).decode("ascii")
    return (await vision_transcribe(b64, OCR_PROMPT)).strip()


async def _ocr_pdf(path: Path) -> tuple[str, int]:
    doc = fitz.open(path)
    try:
        page_count = doc.page_count
        text_layer_pages = [i for i in range(page_count) if len(doc[i].get_text().strip()) >= MIN_TEXT_LAYER_CHARS]
        pages: list[str | None] = [None] * page_count

        if text_layer_pages:
            # pymupdf4llm gives much better markdown (tables, headings) than
            # a raw page.get_text() would, and — since this only runs on
            # pages that already have a real embedded text layer — it's
            # exact, not a transcription, unlike the vision-OCR path below.
            chunks = pymupdf4llm.to_markdown(doc, pages=text_layer_pages, page_chunks=True)
            for chunk in chunks:
                page_num = chunk["metadata"]["page"] - 1  # to_markdown reports 1-based page numbers
                # pymupdf4llm appends its own "-----" page separator to each
                # chunk's text; drop it since page boundaries are already
                # marked below with our own "## Page N" headers.
                text = chunk["text"].rsplit("-----", 1)[0].strip()
                pages[page_num] = text

        image_pages = [i for i in range(page_count) if pages[i] is None]
        for i in image_pages:
            pages[i] = await _vision_ocr_page(doc[i])

        formatted = [f"## Page {i + 1}\n\n{pages[i]}" for i in range(page_count)]
    finally:
        doc.close()
    return "\n\n".join(formatted), page_count
