"""
Shared text-shaping utilities for skill outputs that the citation UI
consumes. Centralized so text / diagram / table / summary records all
hand the front-end the same sanitized, search-ready string format.

The single function that matters here is `build_highlight_text`:
takes a markdown / OCR / model-generated string and produces a plain,
viewer-search-friendly string suitable for PDF.js findController and
for `#search=...` URL fragments.

Hardening applied (in order):

1. Drop DI page markers (`<!-- PageNumber=... -->`, `<!-- PageBreak -->`)
   so they don't pollute search.
2. Unicode NFC normalize. PDFs sometimes embed text in NFD form;
   PDF.js normalizes its search input to NFC so we match.
3. Strip soft-hyphens (U+00AD), zero-width chars, and other invisible
   formatting Unicode that breaks substring matching.
4. Replace smart quotes/dashes with ASCII equivalents. PDFs render
   typographic quotes; the rendered text layer often contains either
   form, but PDF.js's findController treats the search string
   literally — ASCII is a superset hit.
5. Join end-of-line hyphenations: `word-\nrest` → `wordrest` only when
   the next line begins with a lowercase letter (heuristic for soft
   hyphenation; preserves "32-bit", "Class-A" type real hyphens).
6. Strip markdown headers (`# `), list markers (`- `, `* `, `+ `),
   bold (`**...**`) and italic (`*...*`) marker syntax — keep the
   inner text.
7. Replace NBSP (U+00A0) and other Unicode whitespace with a regular
   space, then collapse all whitespace runs to a single ASCII space.
8. Strip C0/C1 control characters (other than space).
9. Cap at 2,000 chars to keep index doc size bounded.

The function is deliberately defensive — every transform tolerates
None / empty / non-string input so callers never crash on bad data
from upstream skills.
"""

from __future__ import annotations

import re
import unicodedata

_PAGE_NUMBER_MARKER_RE = re.compile(
    r'<!--\s*PageNumber\s*=\s*"[^"]*"\s*-->', re.IGNORECASE,
)
_PAGE_BREAK_MARKER_RE = re.compile(r'<!--\s*PageBreak\s*-->', re.IGNORECASE)

_MD_HEADER_RE = re.compile(r"^\s*#{1,6}\s*", re.MULTILINE)
_MD_LIST_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
# Bound the inner span to {1,500} chars (was unbounded `.+?`). With
# re.DOTALL and `.+?`, a stray unbalanced `**` or `*` in OCR'd math
# would force the engine to extend the lazy match across the entire
# remaining text trying every close position -- catastrophic on a
# 2000+ char chunk. The {1,500} bound caps backtrack work at 500
# steps per match attempt. Real bold/italic spans are short (a phrase,
# a number, a word) -- 500 is generous.
_MD_BOLD_RE = re.compile(r"\*\*([^*\n]{1,500}?)\*\*", re.DOTALL)
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)([^*\n]{1,500}?)(?<!\*)\*(?!\*)", re.DOTALL)

# Smart quotes / dashes / other typographic substitutions. Kept as a
# table because the alternatives (regex + lookup function) are slower
# and harder to reason about for a fixed-size mapping.
_TYPOGRAPHIC_MAP = str.maketrans({
    "‘": "'",   # left single quote
    "’": "'",   # right single quote
    "‚": "'",   # single low-9 quote
    "‛": "'",   # single high-reversed-9 quote
    "“": '"',   # left double quote
    "”": '"',   # right double quote
    "„": '"',   # double low-9 quote
    "‟": '"',   # double high-reversed-9 quote
    "′": "'",   # prime
    "″": '"',   # double prime
    "–": "-",   # en dash
    "—": "-",   # em dash
    "−": "-",   # minus sign
    "…": "...", # horizontal ellipsis
    " ": " ",   # non-breaking space
    " ": " ",   # narrow no-break space
    "​": "",    # zero-width space
    "‌": "",    # zero-width non-joiner
    "‍": "",    # zero-width joiner
    "﻿": "",    # BOM
    "­": "",    # soft hyphen
})

_LINE_HYPHEN_RE = re.compile(r"(\w+)-\s*\n\s*([a-z])")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
_WS_RE = re.compile(r"\s+")

MAX_HIGHLIGHT_LEN = 2000


def build_highlight_text(text: str | None) -> str:
    """Produce a plain, viewer-search-friendly version of `text`.

    Returns "" for None / empty / whitespace-only input. Idempotent —
    running it twice gives the same output as running it once.
    """
    if not text:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return ""

    # 1. Drop DI page markers.
    s = _PAGE_NUMBER_MARKER_RE.sub("", text)
    s = _PAGE_BREAK_MARKER_RE.sub("", s)

    # 2. Unicode NFC normalize.
    s = unicodedata.normalize("NFC", s)

    # 3 + 4. Soft-hyphen/zero-width drop + smart-quote → ASCII.
    s = s.translate(_TYPOGRAPHIC_MAP)

    # 5. End-of-line hyphenation join.
    s = _LINE_HYPHEN_RE.sub(r"\1\2", s)

    # 6. Strip markdown syntactic markers.
    s = _MD_HEADER_RE.sub("", s)
    s = _MD_LIST_RE.sub("", s)
    s = _MD_BOLD_RE.sub(r"\1", s)
    s = _MD_ITALIC_RE.sub(r"\1", s)

    # 7. Whitespace collapse (after typographic NBSP→space already done).
    s = _WS_RE.sub(" ", s).strip()

    # 8. Strip control characters last so we don't accidentally drop
    # whitespace we just normalized.
    s = _CONTROL_RE.sub("", s)

    # 9. Hard cap.
    if len(s) > MAX_HIGHLIGHT_LEN:
        s = s[:MAX_HIGHLIGHT_LEN]
    return s
