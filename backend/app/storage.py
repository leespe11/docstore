"""File storage on the mounted volume.

Layout:
    <STORAGE_DIR>/incoming/<uuid><ext>       -- as uploaded, before processing
    <STORAGE_DIR>/<category>/<canonical>     -- final, after classification
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

from slugify import slugify

from .config import settings

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MAX_STEM_LEN = 180


def storage_root() -> Path:
    root = Path(settings.storage_dir)
    (root / "incoming").mkdir(parents=True, exist_ok=True)
    return root


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_incoming(filename: str, data: bytes) -> Path:
    root = storage_root()
    ext = Path(filename).suffix.lower()
    dest = root / "incoming" / f"{uuid.uuid4()}{ext}"
    dest.write_bytes(data)
    return dest


def build_canonical_name(*, suggested_filename: str, doc_date: str | None, ext: str) -> str:
    """Filename = the LLM's own holistic description of the document, slugified,
    with a date prefix only if it has one AND didn't already work it in.

    Deliberately does NOT concatenate category/issuer/doc_type here — those are
    independently-unreliable model outputs, and category is already redundant
    with the folder the file lives in. One well-prompted field beats gluing
    several brittle ones together (see classify.py's `suggested_filename` rule)."""
    base = slugify(suggested_filename) or "document"
    if doc_date:
        compact = doc_date.replace("-", "")
        if doc_date not in base and compact not in base:
            base = f"{doc_date}_{base}"
    if len(base) > _MAX_STEM_LEN:
        base = base[:_MAX_STEM_LEN].rstrip("-")
    return f"{base}{ext}"


def category_dir(category: str | None) -> Path:
    d = storage_root() / (slugify(category or "uncategorized") or "uncategorized")
    d.mkdir(parents=True, exist_ok=True)
    return d


def finalize_file(src: Path, category: str | None, canonical_filename: str) -> Path:
    """Move a processed file from incoming/ into its category folder,
    disambiguating on collision. Also used by reprocessing, where `src` may
    already BE the correctly-placed file (same category, same name) — that's
    not a collision with itself, just a no-op."""
    cat_dir = category_dir(category)
    dest = cat_dir / canonical_filename
    if dest == src:
        return dest

    stem, ext = os.path.splitext(canonical_filename)
    n = 1
    while dest.exists():
        dest = cat_dir / f"{stem}-{n}{ext}"
        n += 1

    src.replace(dest)
    return dest


def sanitize_filename(raw: str, *, fallback_ext: str = "") -> str:
    """Make a user-supplied filename (rename request) safe to put on disk:
    strips path separators/control chars, keeps a real extension, caps length."""
    name = _UNSAFE_CHARS.sub("_", raw.strip())
    name = name.strip(" .")  # Windows disallows trailing dot/space
    if not name:
        name = "document"

    stem, ext = os.path.splitext(name)
    if not ext and fallback_ext:
        ext = fallback_ext
    if len(stem) > _MAX_STEM_LEN:
        stem = stem[:_MAX_STEM_LEN]
    return f"{stem}{ext}"


def storage_relpath(path: Path) -> str:
    return str(Path(path).relative_to(storage_root()))


def absolute_path(relpath: str) -> Path:
    return storage_root() / relpath
