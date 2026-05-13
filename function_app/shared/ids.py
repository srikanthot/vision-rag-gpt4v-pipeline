"""
Stable, explicit chunk IDs with selector-specific prefixes.
"""

import hashlib
import os

SKILL_VERSION = os.environ.get("SKILL_VERSION", "1.0.0")


def _short_hash(value: str, length: int = 12) -> str:
    h = hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()
    return h[:length]


def parent_id_for(source_path: str, source_file: str) -> str:
    base = source_path or source_file or "unknown"
    return _short_hash(base, 16)


def chunk_content_hash(chunk_text: str, length: int = 12) -> str:
    """
    Stable hash of the actual chunk text. Used to disambiguate multiple
    pages produced by SplitSkill for the same layout section.
    """
    return _short_hash(chunk_text or "", length)


def text_chunk_id(
    source_path: str,
    source_file: str,
    layout_ordinal,
    chunk_text: str = "",
) -> str:
    """
    Stable text chunk ID. Layout ordinal locates the section; the chunk
    content hash disambiguates the page within the section. This survives
    re-indexing as long as the chunk text doesn't change.
    """
    pid = parent_id_for(source_path, source_file)
    ord_str = str(layout_ordinal) if layout_ordinal is not None else "0"
    chash = chunk_content_hash(chunk_text)
    return f"txt_{pid}_{ord_str}_{chash}"


def diagram_chunk_id(source_path: str, source_file: str, image_hash: str) -> str:
    pid = parent_id_for(source_path, source_file)
    return f"dgm_{pid}_{image_hash[:16]}"


def table_chunk_id(source_path: str, source_file: str, table_index: str) -> str:
    pid = parent_id_for(source_path, source_file)
    return f"tbl_{pid}_{table_index}"


def table_row_chunk_id(source_path: str, source_file: str, table_index: str, row_index: int) -> str:
    """ID for a per-row table record. Stable as long as the parent
    table_index and row_index are stable (DI's table re-numbering on
    paragraph insertion can break this — same caveat as table_chunk_id)."""
    pid = parent_id_for(source_path, source_file)
    return f"trow_{pid}_{table_index}_{row_index}"


def summary_chunk_id(source_path: str, source_file: str) -> str:
    pid = parent_id_for(source_path, source_file)
    return f"sum_{pid}"


def safe_int(value, default=None):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_str(value, default=""):
    if value is None:
        return default
    return str(value)
