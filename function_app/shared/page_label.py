"""
extract-page-label

Two responsibilities for each text chunk:

  1. Extract the printed page label (the human-visible label, e.g. "iv",
     "3-12", "TOC-2") from the chunk's leading or trailing lines.
  2. Compute the chunk's accurate physical PDF page span by parsing the
     `<!-- PageNumber="N" -->` and `<!-- PageBreak -->` markers that
     DocumentIntelligenceLayoutSkill emits into markdownDocument content
     when outputContentFormat=markdown.

The chunk arrives from SplitSkill operating over a single markdown
section, so we also receive the full section content and the section's
first page number. We locate the chunk inside the section content and
walk the marker timeline to figure out which page it starts on and which
page it ends on.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import threading
from collections import OrderedDict
from typing import Any

from .config import optional_env
from .di_client import fetch_cached_analysis
from .ids import (
    SKILL_VERSION,
    parent_id_for,
    safe_int,
    safe_str,
    text_chunk_id,
)


def _embedding_version() -> str:
    """Return the embedding model identifier for this index. Sourced from
    env so a model upgrade (ada-002 → text-embedding-3-large) bumps the
    field without code changes; the orchestrator can then prefer-rank
    rows with the current embedding_version."""
    return optional_env("EMBEDDING_MODEL_VERSION", "text-embedding-ada-002")


# Equipment-id detection. MANGOS manuals reference equipment by manufacturer
# part numbers ("GE-THQL-1120-2"), model numbers ("ABB-VD4-1250"), and
# class codes ("NEMA 4X"). The single most common chatbot query shape on
# technical manuals is "what does manual X say about model Y", so
# extracting these as a filterable collection enables exact-match lookup
# without going through full-text search.
_EQUIPMENT_ID_RE = re.compile(
    # 2-5 letters, hyphen, then alphanumeric chunks separated by hyphens.
    # Requires at least one digit somewhere in the right-hand portion to
    # avoid matching all-letter words ("NEMA-style").
    #
    # Bounded quantifiers throughout. The previous form had an unbounded
    # `[A-Z0-9]*` followed by `\d[A-Z0-9-]{0,8}` — combined with the
    # leading `(?:[A-Z0-9]{1,8}-){0,3}`, the regex engine could backtrack
    # quadratically on hyphenated equipment-heavy chunks (typical MANGOS
    # manual paragraph). Tightening the trailing run to `{0,8}` makes the
    # whole pattern linear-time in chunk length.
    r"\b[A-Z]{2,5}-(?:[A-Z0-9]{1,8}-){0,3}[A-Z0-9]{0,8}\d[A-Z0-9-]{0,8}\b"
)


def _extract_equipment_ids(text: str) -> list[str]:
    if not text:
        return []
    return sorted({
        m.group(0).rstrip(".,;:")
        for m in _EQUIPMENT_ID_RE.finditer(text)
    })


def _detect_language(text: str) -> str:
    """Best-effort language code for the chunk.

    Cheap heuristic — looks for English stop-words. This is enough for
    today's MANGOS corpus (English-only) but lets a future Spanish or
    French manual flag itself for filter routing without a full
    language-detect dependency. Returns ISO 639-1 codes when detected,
    "" when undetermined."""
    if not text or len(text) < 30:
        return ""
    sample = text[:500].lower()
    en_markers = (" the ", " and ", " of ", " for ", " with ", " is ")
    es_markers = (" el ", " la ", " los ", " las ", " que ", " para ")
    fr_markers = (" le ", " la ", " les ", " des ", " pour ", " avec ")
    en = sum(1 for m in en_markers if m in sample)
    es = sum(1 for m in es_markers if m in sample)
    fr = sum(1 for m in fr_markers if m in sample)
    best = max(en, es, fr)
    if best == 0:
        return ""
    if en == best:
        return "en"
    if es == best:
        return "es"
    return "fr"


def _compute_quality_score(
    *,
    page_resolution_method: str,
    chunk_len: int,
    has_headers: bool,
    is_toc_like: bool,
    has_callouts: bool,
    has_figure_or_table_ref: bool,
) -> float:
    """Composite chunk-quality score in [0.0, 1.0]. Used as a tie-breaker
    when the semantic ranker can't distinguish two near-equal hits.

    Signals (each weighted by how strongly it predicts retrieval value):
      - page_resolution_method: di_input/header_match (high) vs missing (low)
      - chunk_len: too-short chunks under-perform; very long chunks
        risk being a TOC dump or boilerplate
      - has_headers: header chain attached -> strong context
      - is_toc_like: penalty (these are filtered, but the score reflects
        weakness if a TOC sneaks past the heuristic)
      - has_callouts / has_figure_or_table_ref: retrieval-rich content
    """
    score = 0.0
    method_weight = {
        "di_input": 0.30,
        "header_match": 0.30,
        "fuzzy_match": 0.20,
        "paragraph_bbox": 0.20,
        "bbox_corrected": 0.25,
        "missing": 0.0,
        "document_summary": 0.20,
    }.get(page_resolution_method, 0.10)
    score += method_weight
    if has_headers:
        score += 0.20
    if 200 <= chunk_len <= 3000:
        score += 0.20
    elif 100 <= chunk_len < 200:
        score += 0.10
    if has_callouts:
        score += 0.10
    if has_figure_or_table_ref:
        score += 0.10
    if is_toc_like:
        score -= 0.30
    # Clamp to [0, 1].
    return round(max(0.0, min(1.0, score)), 3)


def _approx_token_count(text: str) -> int:
    """Cheap token-count approximation for prompt-budget math without
    pulling in a tokenizer. ~4 chars per token for English text matches
    BPE tokenizers (cl100k_base) within ~10%, which is enough precision
    for the orchestrator to decide "can I fit 8 chunks in a 4k window?"
    without the runtime cost of a real tokenizer call."""
    if not text:
        return 0
    # Round up so an empty-after-strip chunk still reports 0, and a
    # 1-char chunk reports 1.
    return max(0, (len(text) + 3) // 4)
from .sections import build_section_index
from .text_utils import build_highlight_text

# ---------- printed-label heuristics ----------

ROMAN_RE = re.compile(r"\b([ivxlcdm]{1,6})\b", re.IGNORECASE)
PAGE_PREFIX_RE = re.compile(r"\bpage\s+([A-Za-z0-9\-\.]{1,8})\b", re.IGNORECASE)


# Validates a candidate page label looks like a real page number, not a
# random word. Real page labels:
#   "215" / "12" — pure digits
#   "5-7" / "18.33" — chapter-prefixed numerics
#   "A-12" / "TOC-3" — letter-prefixed numerics
#   "iv" / "xiii" — roman numerals
# Words like "refers", "Cover", "Notes", "the", etc. are rejected.
# Without this validation, the label heuristics overmatch on body text
# words that happen to follow "page " (e.g. "page refers to N").
_LABEL_VALID_RE = re.compile(
    r"^("
    r"[ivxlcdm]+"                                # roman numerals (e.g. iv, xiii)
    r"|[A-Z]{0,4}[\-\.]?\d{1,4}([\-\.]\d{1,4}){0,2}"  # 215, 5-7, A-12, TOC-3
    r")$",
    re.IGNORECASE,
)


def _is_valid_label(s: str) -> bool:
    """True if `s` looks like a real page label (digits, chapter-prefixed,
    or roman). Used to reject obvious false positives like 'refers' or
    'Cover' that the loose heuristics might otherwise return."""
    if not s:
        return False
    return bool(_LABEL_VALID_RE.fullmatch(s.strip()))
DASH_NUM_RE = re.compile(
    r"^[\-\u2013\u2014\s]*("
    r"[A-Za-z]{1,3}[\-\.]\d{1,4}"   # A-7, B.4
    r"|\d{1,4}[\-\.]\d{1,4}"        # 3-4, 3.4
    r"|\d{1,4}"                     # 12
    r")[\-\u2013\u2014\s]*$"
)
SECTION_DASH_RE = re.compile(r"\b([A-Z]{1,3}[\-\.]\d{1,4})\b")
TOC_LIKE_RE = re.compile(r"\b(TOC|Index|Form|Fig|Table|App)[\-\s]?(\d{1,4})\b", re.IGNORECASE)

# Matches figure references like "Figure 4-2", "Fig. 12", "FIGURE A-1".
# The reference ID must contain at least one digit to avoid false positives
# from DI word-splitting (e.g. "Fig\nure" → "Fig" + "ure").
FIGURE_REF_RE = re.compile(
    r"\b(Figure|Fig\.?)\s*[\-:]?\s*([A-Z]{0,3}[\-\.]?\d[\w\-\.]{0,8})",
    re.IGNORECASE,
)

# Matches table references like "Table 18-3", "Tbl. 4", "TABLE A-1".
# Same shape as FIGURE_REF_RE; we extract these so a chunk that says
# "see Table 18-3 for fuse ratings" carries that anchor as a searchable
# field, not just as inline body text. Mirrors what we do for figures.
TABLE_REF_RE = re.compile(
    r"\b(Table|Tbl\.?)\s*[\-:]?\s*([A-Z]{0,3}[\-\.]?\d[\w\-\.]{0,8})",
    re.IGNORECASE,
)

# Section-number references: "Section 4.2", "Sec. 18-3", "§ 4.2.1".
# Captures just the number portion (e.g. "4.2") so the collection field
# can be queried as `sections_referenced/any(s: s eq '4.2')`. We do NOT
# require a leading word boundary because `§` is a non-word character.
SECTION_REF_RE = re.compile(
    r"(?:Section|Sec\.?|§)\s*([0-9]+(?:[\.\-][0-9]+){0,3})\b",
    re.IGNORECASE,
)

# Page references: "page 18-25", "p. 215", "pp. 18-25", "page A-7".
# Captures the page label (e.g. "18-25"). We accept both digit-only
# and chapter-prefixed labels because MANGOS manuals use both.
PAGE_REF_RE = re.compile(
    r"\b(?:page|pages|pp?\.?)\s+([A-Z]{0,3}[\-\.]?\d[\w\-\.]{0,8})",
    re.IGNORECASE,
)


# Heuristic for detecting glossary / definitions / acronyms chunks. Match
# on the chunk's header chain (h1/h2/h3) — these sections are usually
# tagged clearly in the manual's TOC. We also check for "definition" lines
# in the body as a content-side signal for chunks that DI didn't tag with
# a clean header. The result populates `record_subtype="glossary"` so a
# chatbot answering "what does X mean?" can prefer-rank glossary entries
# over body prose that happens to mention X.
GLOSSARY_HEADER_RE = re.compile(
    r"\b(glossary|definitions?|acronyms?|abbreviations?|nomenclature|terminology)\b",
    re.IGNORECASE,
)
# Body-side signal: lines like "TERM:" or "TERM —" followed by a definition.
# Looks for at least 3 consecutive such lines (a glossary in flight). The
# term character class allows letters, digits, ampersand, parens, slash,
# dot, comma, apostrophe, and hyphen — covers common glossary terms like
# "PSE&G", "I/O", "120/240V", "Section 4.2", "L1 (line 1)".
GLOSSARY_LINE_RE = re.compile(
    r"^\s*[A-Z][\w &()/.,'\-]{1,40}\s*[:\-—–]\s+\S",
    re.MULTILINE,
)


def _is_glossary_chunk(text: str, h1: str, h2: str, h3: str) -> bool:
    """True if the chunk is glossary content. Header match wins; body
    pattern match (≥3 definition-style lines) is a fallback."""
    for h in (h1, h2, h3):
        if h and GLOSSARY_HEADER_RE.search(h):
            return True
    if text:
        # Count definition-style lines. ≥3 indicates a glossary in flight.
        # Lower threshold than _is_toc_like because glossary chunks are
        # often shorter and more uniform.
        matches = GLOSSARY_LINE_RE.findall(text)
        if len(matches) >= 3:
            return True
    return False


# Heuristic for detecting Table-of-Contents / List-of-Figures style chunks:
# lines like "Section title ............... 18-3" with dot-leaders followed
# by a page reference. We don't want these polluting top-of-results, so we
# stamp them with processing_status="toc_like" instead of "ok" and let the
# UI / query layer filter on processing_status.
# Bounded `.+?` -> `.{4,200}?` so the lazy prefix can't extend across
# the entire line on pathological inputs (URLs, version strings, dotted
# namespaces). Combined with cap on trailing token, runs in linear time
# even on long lines.
TOC_LEADER_LINE_RE = re.compile(
    r"^.{4,200}?(?:\.\s*){3,}\s*[\dA-Z][\w\-\.]{0,8}\s*$",
)

# Relaxed variant: a line that ENDS with a page-pointer (e.g. "18-3", "215",
# "A-7"), regardless of whether dot-leaders are present. Catches:
#   - Tab-aligned TOCs that DI rendered as columns of whitespace
#   - List-of-Figures pages with multi-line caption wraps
#   - Back-of-book index lines: "Breaker, molded-case . . . 18-3, 18-7"
#
# More forgiving than the dot-leader regex but still anchored on the trailing
# page reference to keep false positives off body content.
TOC_TAILING_PAGE_RE = re.compile(
    r"^.{4,}?\s+[A-Z]?\d[\w\-\.]{0,8}(?:\s*,\s*[A-Z]?\d[\w\-\.]{0,8})*\s*$"
)


def _is_toc_like(text: str) -> bool:
    """True if the chunk reads as a TOC / list-of-figures / index page.

    Two-tier detection:
      - Strict: dot-leader lines (".....18-3"). High precision.
      - Relaxed: lines ending with a page-pointer (and no other content
        signal). Catches tab-aligned TOCs and back-of-book indexes.

    Tier requirements:
      - At least 5 lines that match either tier
      - >= 60% of non-empty lines match (ratio guard against body-text
        chunks that contain a few page pointers but read as prose)

    A real body chunk almost never crosses both bars; a TOC / index
    chunk almost always does.
    """
    if not text:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 5:
        return False
    strict = sum(1 for ln in lines if TOC_LEADER_LINE_RE.match(ln))
    relaxed = sum(1 for ln in lines if TOC_TAILING_PAGE_RE.match(ln))
    matches = max(strict, relaxed)
    if matches < 5:
        return False
    return (matches / len(lines)) >= 0.6


# ---------- DI markdown page markers ----------
# DI emits both forms: <!-- PageNumber="3" --> and <!-- PageBreak -->
# PageNumber content is the *printed* label (may be "3", "iv", "18-33"),
# so we capture everything between the quotes as a string.
PAGE_NUMBER_MARKER_RE = re.compile(r'<!--\s*PageNumber\s*=\s*"([^"]*)"\s*-->', re.IGNORECASE)
PAGE_BREAK_MARKER_RE = re.compile(r'<!--\s*PageBreak\s*-->', re.IGNORECASE)


# ---------- DI-cache fallback for section_start_page ----------
#
# The skillset currently wires `/document/markdownDocument/*/pageNumber` as
# the section's starting physical page, but DocumentIntelligenceLayoutSkill
# does not reliably expose that field. When the input arrives as None,
# we look up the DI cache blob (written by preanalyze.py), build a section
# index from the raw analyzeResult, and match the chunk's section_content
# to its source section to recover page_start.
#
# Cached at module scope so a batch of chunks from the same PDF triggers
# at most one blob fetch.

# LRU-style bound: a Function App instance that has processed many PDFs
# in one lifetime (e.g. after a long indexer run) would otherwise hold
# every section index it has ever seen in memory. Cap at 20 PDFs.
#
# Both caches are guarded by `_CACHE_LOCK` because Azure Functions can
# process multiple requests concurrently in the same worker process,
# and OrderedDict's move_to_end / popitem are not thread-safe. Without
# the lock, two concurrent skill calls hitting the same fresh PDF could
# corrupt the dict (one popitem mid-write of the other) and produce
# wrong section data on subsequent lookups for that worker.
_SECTION_INDEX_CACHE: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
_ANALYSIS_CACHE: "OrderedDict[str, dict[str, Any] | None]" = OrderedDict()
# Bumped 5 -> 32. Audit 2026-05-12 found that on a 50-PDF indexer batch
# with chunks interleaved across PDFs (the normal case under
# degreeOfParallelism > 1), the bound-5 caches evicted constantly:
# PDF #6's first chunk evicted PDF #1, then PDF #1's NEXT chunk rebuilt
# the derived structures (full paragraph scan), evicting PDF #2, etc.
# Net effect: the per-PDF "build once" optimization degraded to "build
# every chunk". Bound of 32 fits one full batch in memory; each cached
# entry is ~30-100MB (paragraphs_by_page etc.), so worst-case ~3.2GB
# memory budget — acceptable on Premium plan workers (~14GB). Drop
# to a smaller value if Consumption-plan workers are the deploy target.
_SECTION_INDEX_CACHE_MAX = 32
_CACHE_LOCK = threading.Lock()


_FAILURE_TTL_SECONDS = 30.0  # cache None for 30s, then retry

# Failure-time tracker so we can implement a time-bounded retry.
_ANALYSIS_FAILURE_AT: dict[str, float] = {}

# Per-source_path fetch locks. When a chunk-batch from the same PDF
# arrives in parallel (the indexer fans 100+ chunks out per PDF), the
# original implementation let every thread hit storage at once -- 100
# concurrent GETs on the same 60KB blob caused storage queueing and
# every fetch ran into its 120s timeout. With per-PDF locks, exactly
# ONE thread fetches per source_path; the rest wait briefly and read
# the populated cache, eliminating the thundering-herd hang.
#
# SHARDED FIXED-SIZE LOCK ARRAY (was OrderedDict with LRU eviction).
# The LRU eviction had a SUBTLE RACE: when the dict evicted a lock that
# a thread was about to acquire, a new caller for the same source_path
# would create a FRESH lock object, defeating mutual exclusion. Two
# threads could then rebuild the same cache concurrently -- 30M+
# paragraph iterations × 2, easily OOMing on huge PDFs. The fix is to
# NEVER evict a lock; use a fixed-size array indexed by
# `hash(source_path) % _LOCK_SHARDS`. Two distinct PDFs may share a
# lock (rare collision) but they only serialize -- never lose mutual
# exclusion. Memory cost: 32 locks × 3 dicts × ~50 bytes = ~5 KB total.
_LOCK_SHARDS = 32
_FETCH_LOCKS: list[threading.Lock] = [threading.Lock() for _ in range(_LOCK_SHARDS)]

# Per-PDF derived-structures cache. Per the deep audit on 2026-05-12,
# process_page_label was scanning ALL paragraphs of the DI cache 6+ times
# PER CHUNK — for huge PDFs (10K paragraphs, 500 chunks) this was 30M+
# paragraph iterations + 5M+ regex calls, easily blowing the 230s Azure
# WebApi skill timeout. The fix: derive everything that's constant per
# source_path ONCE on first call, then look up via dict access on
# subsequent chunks. _DERIVED_CACHE holds:
#   - cover_meta:           dict {document_revision, effective_date, document_number}
#   - min_conf_by_page:     dict {page_number: float | None}
#   - paragraphs_by_page:   dict {page_number: list[paragraph]}
#   - pagenumber_by_page:   dict {page_number: list[paragraph] with role=pageNumber}
#   - pagefurniture_by_page:dict {page_number: list[paragraph] with role in pageFooter/pageHeader}
# Eviction follows the same LRU bound as _ANALYSIS_CACHE.
_DERIVED_CACHE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

# Build-serialization locks: separate from _FETCH_LOCKS (which is held
# while _analysis_for fetches the DI cache). If _derived_for or
# _sections_for reused _FETCH_LOCKS and then called _analysis_for inside,
# we'd deadlock on the same source_path. Three independent shard arrays.
_DERIVED_BUILD_LOCKS: list[threading.Lock] = [threading.Lock() for _ in range(_LOCK_SHARDS)]
_SECTIONS_BUILD_LOCKS: list[threading.Lock] = [threading.Lock() for _ in range(_LOCK_SHARDS)]


def _lock_index(key: str) -> int:
    """Deterministic shard index for a source_path. Python's built-in
    `hash` is salted per-process but stable within a worker lifetime —
    that's exactly what we need (don't care about cross-worker consistency
    since each worker has its own lock array)."""
    return hash(key) % _LOCK_SHARDS


def _get_derived_build_lock(source_path: str) -> threading.Lock:
    return _DERIVED_BUILD_LOCKS[_lock_index(source_path)]


def _get_sections_build_lock(source_path: str) -> threading.Lock:
    return _SECTIONS_BUILD_LOCKS[_lock_index(source_path)]


def _get_fetch_lock(source_path: str) -> threading.Lock:
    return _FETCH_LOCKS[_lock_index(source_path)]


def _derived_for(source_path: str) -> dict[str, Any]:
    """Return derived per-PDF structures, building once and caching.

    Without this, process_page_label scanned the full paragraphs[] array
    of the DI cache 6+ times PER CHUNK. On huge PDFs (10K paragraphs,
    500 chunks) that's 30M+ iterations + 5M+ regex calls.
    With this cache each chunk gets O(1) dict lookups instead.

    Cached structures:
      - cover_meta: dict (document_revision, effective_date, document_number)
      - min_conf_by_page: {page_num: float|None}
      - paragraphs_by_page: {page_num: [para,...]}
      - pagenumber_by_page: {page_num: [para with role=pageNumber]}
      - pagefurniture_by_page: {page_num: [para with role in pageFooter/pageHeader]}
      - text_bbox_paragraphs: [(content, content_norm, page, bbox), ...]
        — paragraphs ≥40 chars, pre-normalized, for _text_bbox_for_chunk
    """
    # Fast path: cache hit, no per-PDF lock needed.
    with _CACHE_LOCK:
        cached = _DERIVED_CACHE.get(source_path)
        if cached is not None:
            _DERIVED_CACHE.move_to_end(source_path)
            return cached
    # Slow path: serialize per source_path so 100 parallel chunks don't
    # each rebuild the derived structure (10K paragraph iteration + 10K
    # _normalize_text calls). Only one thread builds; others wait briefly
    # and read the populated cache.
    build_lock = _get_derived_build_lock(source_path)
    with build_lock:
        # Re-check cache: while we waited, the designated builder may
        # have populated it.
        with _CACHE_LOCK:
            cached = _DERIVED_CACHE.get(source_path)
            if cached is not None:
                _DERIVED_CACHE.move_to_end(source_path)
                return cached
        result = _analysis_for(source_path)
        derived: dict[str, Any] = {
            "cover_meta": {"document_revision": "", "effective_date": "", "document_number": ""},
            "min_conf_by_page": {},
            "paragraphs_by_page": {},
            "pagenumber_by_page": {},
            "pagefurniture_by_page": {},
            "text_bbox_paragraphs": [],
        }
        if result:
            # Cover metadata — built once, reused across every chunk.
            try:
                derived["cover_meta"] = cover_metadata_from_analyze(result)
            except Exception as exc:
                logging.warning("page_label: cover_meta build failed for %s: %s",
                                source_path, exc)
            # Index paragraphs by their physical_pdf_page (from boundingRegions[0])
            # so per-chunk lookups become O(paras_on_page) instead of O(all_paras).
            paragraphs = result.get("paragraphs") or []
            paras_by_page: dict[int, list[dict[str, Any]]] = {}
            pgnum_by_page: dict[int, list[dict[str, Any]]] = {}
            pgfurn_by_page: dict[int, list[dict[str, Any]]] = {}
            # text_bbox candidates: pre-normalized + bbox extracted ONCE.
            bbox_candidates: list[tuple[str, str, int, tuple[float, float, float, float]]] = []
            for para in paragraphs:
                content = (para.get("content") or "").strip()
                for region in (para.get("boundingRegions") or []):
                    pn = region.get("pageNumber")
                    if isinstance(pn, int) and pn > 0:
                        paras_by_page.setdefault(pn, []).append(para)
                        role = (para.get("role") or "").lower()
                        if role == "pagenumber":
                            pgnum_by_page.setdefault(pn, []).append(para)
                        elif role in ("pagefooter", "pageheader"):
                            pgfurn_by_page.setdefault(pn, []).append(para)
                        # Pre-normalize + extract bbox once for text_bbox lookup
                        if content and len(content) >= 40:
                            norm = _normalize_text(content)
                            if norm and len(norm) >= 40:
                                polygon = region.get("polygon") or []
                                bb = _bbox_from_polygon(polygon)
                                if bb is not None:
                                    bbox_candidates.append((content, norm, pn, bb))
                        break  # first region's page is sufficient
            derived["paragraphs_by_page"] = paras_by_page
            derived["pagenumber_by_page"] = pgnum_by_page
            derived["pagefurniture_by_page"] = pgfurn_by_page
            derived["text_bbox_paragraphs"] = bbox_candidates
            # Per-page min OCR confidence — once per PDF.
            di_pages = result.get("pages") or []
            min_by_page: dict[int, float | None] = {}
            for page in di_pages:
                pn = page.get("pageNumber")
                if not isinstance(pn, int) or pn <= 0:
                    continue
                min_conf: float | None = None
                for word in (page.get("words") or []):
                    conf = word.get("confidence")
                    if isinstance(conf, (int, float)):
                        if min_conf is None or conf < min_conf:
                            min_conf = float(conf)
                min_by_page[pn] = min_conf
            derived["min_conf_by_page"] = min_by_page
        with _CACHE_LOCK:
            _DERIVED_CACHE[source_path] = derived
            while len(_DERIVED_CACHE) > _SECTION_INDEX_CACHE_MAX:
                _DERIVED_CACHE.popitem(last=False)
        return derived


def _analysis_for(source_path: str) -> dict[str, Any] | None:
    """Fetch and cache the raw DI analyzeResult for a PDF.

    Returned shape is the analyzeResult itself (so callers can read
    `paragraphs`, `pages`, `sections`, etc. uniformly without the
    top-level `analyzeResult` wrapper).

    Critical fix (2026-05-07): a failed fetch is no longer cached
    indefinitely. Previously a transient cache-fetch failure on the
    first call (timeout, race, cold-start blob throttle) would store
    None for the source_path and EVERY subsequent call from the same
    function-app instance would return None instantly without retrying.
    With Sprint 6's parity additions, diagrams/tables now also call
    this helper -- so a single failed fetch during diagram processing
    would poison the cache and text-record processing would all fall
    to "missing".

    Behavior now:
      - Successful results: cached indefinitely (until LRU eviction).
      - Failures: cached as None with a 30-second TTL. After 30s the
        cached None expires and the next call retries. This both:
          * prevents thundering herd retries from many parallel chunks
            on the same source_path (within the 30s window)
          * recovers automatically if the failure was transient
        Tests using synthetic URLs that always fail are unaffected
        because they typically run < 30s and the cached None persists
        for the duration of the test.
    """
    if not source_path:
        return None
    import time as _time
    now = _time.time()
    # Fast path: cache hit with no per-PDF lock contention.
    with _CACHE_LOCK:
        if source_path in _ANALYSIS_CACHE:
            cached = _ANALYSIS_CACHE[source_path]
            if cached is not None:
                _ANALYSIS_CACHE.move_to_end(source_path)
                return cached
            # Cached None: check TTL. If still within failure window,
            # return None without re-fetching.
            failed_at = _ANALYSIS_FAILURE_AT.get(source_path, 0.0)
            if now - failed_at < _FAILURE_TTL_SECONDS:
                return None
            # TTL expired -- fall through to fetch (under per-PDF lock).
    # Slow path: serialize fetches per source_path so 100 parallel chunks
    # don't all hit storage at once. The first thread fetches; others
    # wait briefly then read the populated cache.
    fetch_lock = _get_fetch_lock(source_path)
    with fetch_lock:
        # Re-sample `now` AFTER acquiring fetch_lock. A late-arriving
        # thread may have waited many seconds on this lock; the outer
        # `now` would then under-count the elapsed-since-failure window,
        # causing the TTL re-check to return None when it should retry.
        now = _time.time()
        # Re-check cache: while we waited on the per-PDF lock, the
        # designated fetcher may have already populated it.
        with _CACHE_LOCK:
            if source_path in _ANALYSIS_CACHE:
                cached = _ANALYSIS_CACHE[source_path]
                if cached is not None:
                    _ANALYSIS_CACHE.move_to_end(source_path)
                    return cached
                failed_at = _ANALYSIS_FAILURE_AT.get(source_path, 0.0)
                if now - failed_at < _FAILURE_TTL_SECONDS:
                    return None
                _ANALYSIS_CACHE.pop(source_path, None)
                _ANALYSIS_FAILURE_AT.pop(source_path, None)
        try:
            analyze = fetch_cached_analysis(source_path)
            result = None
            if analyze:
                result = analyze.get("analyzeResult") if isinstance(analyze, dict) else None
                result = result or analyze
        except Exception as exc:
            logging.warning("page_label: failed to fetch DI cache for %s: %s",
                            source_path, exc)
            result = None
        # Stamp the failure timestamp using the most recent wall clock so
        # the TTL window starts from when the fetch actually failed.
        now = _time.time()
        with _CACHE_LOCK:
            _ANALYSIS_CACHE[source_path] = result
            if result is None:
                # Track failure timestamp for TTL-based retry.
                _ANALYSIS_FAILURE_AT[source_path] = now
                logging.warning(
                    "page_label: _analysis_for returned None for %s "
                    "(will retry after %ds)",
                    (source_path or "")[-60:], int(_FAILURE_TTL_SECONDS),
                )
            else:
                _ANALYSIS_FAILURE_AT.pop(source_path, None)
            # Evict oldest while over capacity. While loop in case multiple
            # concurrent insertions all overshot before we grabbed the lock.
            while len(_ANALYSIS_CACHE) > _SECTION_INDEX_CACHE_MAX:
                _ANALYSIS_CACHE.popitem(last=False)
    return result


def _sections_for(source_path: str) -> list[dict[str, Any]]:
    if not source_path:
        return []
    # Fast path: cache hit.
    with _CACHE_LOCK:
        cached = _SECTION_INDEX_CACHE.get(source_path)
        if cached is not None:
            _SECTION_INDEX_CACHE.move_to_end(source_path)
            return cached
    # Slow path: per-PDF lock so 100 parallel chunks don't all rebuild
    # section_index when sidecar is missing. build_section_index takes
    # 3-5 min on 2700+-section PDFs; 100 concurrent builds = worker death.
    build_lock = _get_sections_build_lock(source_path)
    with build_lock:
        # Re-check cache: while we waited, the designated builder may
        # have populated it.
        with _CACHE_LOCK:
            cached = _SECTION_INDEX_CACHE.get(source_path)
            if cached is not None:
                _SECTION_INDEX_CACHE.move_to_end(source_path)
                return cached
        # FAST PATH: pre-built sidecar from preanalyze.
        sections: list[dict[str, Any]] = []
        try:
            from .di_client import fetch_cached_sections
            sidecar = fetch_cached_sections(source_path)
            if sidecar is not None and isinstance(sidecar, list):
                sections = sidecar
                logging.info(
                    "page_label: loaded %d sections from sidecar for %s",
                    len(sections), (source_path or "")[-60:],
                )
        except Exception as exc:
            logging.warning("page_label: sidecar load failed for %s: %s",
                            source_path, exc)
        # FALLBACK: build live from DI cache if sidecar missing/empty.
        if not sections:
            result = _analysis_for(source_path)
            if result:
                try:
                    sections = build_section_index(result)
                    logging.info(
                        "page_label: built %d sections live (sidecar miss) for %s",
                        len(sections), (source_path or "")[-60:],
                    )
                except Exception as exc:
                    logging.warning("page_label: failed to build sections for %s: %s",
                                    source_path, exc)
                    sections = []
        # Pre-normalize headers and content ONCE here so per-chunk
        # _find_section_start_page lookups become dict reads instead of
        # 12M regex calls per PDF (4M sections × 3 headers × 500 chunks).
        # Stamps `_h1n/_h2n/_h3n/_content_norm` on each section dict.
        # _content_norm is capped to 400 chars to bound memory for very
        # long sections; _find_section_start_page's probe is also 400.
        _norm_ws = re.compile(r"\s+")
        for s in sections:
            s["_h1n"] = _norm_ws.sub(" ", (s.get("header_1") or "").strip())
            s["_h2n"] = _norm_ws.sub(" ", (s.get("header_2") or "").strip())
            s["_h3n"] = _norm_ws.sub(" ", (s.get("header_3") or "").strip())
            s["_content_norm"] = _normalize_text(s.get("content") or "")[:400]
        with _CACHE_LOCK:
            _SECTION_INDEX_CACHE[source_path] = sections
            while len(_SECTION_INDEX_CACHE) > _SECTION_INDEX_CACHE_MAX:
                _SECTION_INDEX_CACHE.popitem(last=False)
        return sections


def _pdf_total_pages_for(source_path: str) -> int | None:
    """Total physical page count of the source PDF (from DI cache).

    Used by UIs to render '<page X> of <total>' citations and to
    detect drift when our computed `physical_pdf_page` exceeds the
    actual PDF length.
    """
    result = _analysis_for(source_path)
    if not result:
        return None
    pages = result.get("pages") or []
    return len(pages) if pages else None


def _page_dimensions_for(source_path: str, physical_page: int | None) -> tuple[float, float] | None:
    """Return (width_in, height_in) of the given physical page from DI's
    pages[] array. Falls back to US Letter (8.5 x 11) when DI cache is
    unreachable or page index is out of range.

    Used by the whole-page-bbox fallback below."""
    if physical_page is None or physical_page < 1:
        return (8.5, 11.0)
    if not source_path:
        return (8.5, 11.0)
    result = _analysis_for(source_path)
    if not result:
        return (8.5, 11.0)
    pages = result.get("pages") or []
    if not pages or physical_page > len(pages):
        return (8.5, 11.0)
    page = pages[physical_page - 1]
    width = page.get("width")
    height = page.get("height")
    unit = (page.get("unit") or "inch").lower()
    if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
        return (8.5, 11.0)
    # DI emits dimensions in the unit specified per page. Convert to inches.
    if unit in ("inch", "inches"):
        return (float(width), float(height))
    if unit in ("pixel", "pixels"):
        # 96 DPI assumption for pixel measurements
        return (float(width) / 96.0, float(height) / 96.0)
    if unit in ("centimeter", "cm"):
        return (float(width) / 2.54, float(height) / 2.54)
    return (float(width), float(height))


def _whole_page_bbox(source_path: str, physical_page: int) -> dict[str, Any]:
    """Build a single bbox covering the entire physical page. Used as a
    last-resort fallback when:
      - we know the chunk's physical_pdf_page (via header_match,
        bbox_corrected, etc.)
      - BUT the strict paragraph matcher returned no per-paragraph bboxes
        (chunks with fragmentary content like form data, GIS layer
        names, single-word lists)
    The frontend can render a faint full-page highlight in this case
    rather than no highlight at all. Better than empty text_bbox."""
    width_in, height_in = _page_dimensions_for(source_path, physical_page) or (8.5, 11.0)
    return {
        "page": physical_page,
        "x_in": 0.0,
        "y_in": 0.0,
        "w_in": round(width_in, 3),
        "h_in": round(height_in, 3),
    }


def _printed_label_for_page(source_path: str, physical_page: int | None) -> str | None:
    """Look up the printed page label for a specific physical page by
    scanning DI's pageNumber/pageFooter/pageHeader role paragraphs in
    the cached analyzeResult.

    DI tags page-furniture paragraphs with `role` ∈ {pageNumber,
    pageFooter, pageHeader, pageBreak}. The pageNumber role, when
    present, holds exactly the printed label ("5-7", "iv", "A-12").
    Footers/headers may carry it inline among other text; we extract
    it via the same heuristic as `_extract_label`.

    This catches chapter-prefixed labels like "5-7" that the chunk
    body doesn't contain (they live in the page footer, which DI
    extracts as a separate paragraph with role=pageFooter — not
    visible to the SplitSkill chunk).

    Returns None when no label can be recovered. Callers should fall
    back to synthesizing from the physical page number in that case.
    """
    if physical_page is None or not source_path:
        return None
    # Use pre-indexed paragraphs by page (built once per source_path) to
    # avoid scanning all 10K+ paragraphs of a huge DI cache on every
    # chunk. Was 2 full paragraph passes per call × 2 calls (start/end)
    # × hundreds of chunks per PDF = 4 × pdf_chunks × all_paragraphs.
    derived = _derived_for(source_path)
    pagenumber_paras = derived["pagenumber_by_page"].get(physical_page, [])
    pagefurniture_paras = derived["pagefurniture_by_page"].get(physical_page, [])

    # First pass: pageNumber-role paragraphs are the canonical source.
    # Validate against the printed-label shape (DI sometimes mis-tags
    # arbitrary text with role=pageNumber).
    for para in pagenumber_paras:
        content = (para.get("content") or "").strip()
        if content and _is_valid_label(content):
            return content

    # Second pass: pageFooter / pageHeader on this page often look like
    # "Chapter 5 — Meters | 5-7"; extract label via heuristic.
    for para in pagefurniture_paras:
        content = (para.get("content") or "").strip()
        if not content:
            continue
        label = _extract_label(content)
        if label:
            return label

    return None


# ---------- cover-page metadata ----------
#
# These regexes are used to mine page-1 paragraphs for document-level
# fields the chatbot must know to answer "is this the current revision?".
# Tolerant: MANGOS manuals format these many ways across the corpus.
_REVISION_RE = re.compile(
    r"\b(?:rev(?:ision)?|version|issue|ver)\.?\s*[:\-]?\s*([A-Z0-9][\w\.\-]{0,15})",
    re.IGNORECASE,
)
_DOC_NUMBER_RE = re.compile(
    r"\b(?:document\s*(?:number|no\.?)|doc\s*(?:no\.?|#)|publication\s*(?:no\.?|number))\s*[:\-]?\s*([A-Z0-9][\w\-]{2,30})",
    re.IGNORECASE,
)
# Date matchers — try month-year, ISO, and slash formats. We emit ISO 8601
# (YYYY-MM or YYYY-MM-DD) so the index field is sort/filter-friendly.
_MONTHS = {
    "jan": "01", "january": "01", "feb": "02", "february": "02",
    "mar": "03", "march": "03", "apr": "04", "april": "04",
    "may": "05", "jun": "06", "june": "06", "jul": "07", "july": "07",
    "aug": "08", "august": "08", "sep": "09", "sept": "09", "september": "09",
    "oct": "10", "october": "10", "nov": "11", "november": "11",
    "dec": "12", "december": "12",
}
_DATE_MONTH_YEAR_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)
_DATE_MONTH_YEAR_SHORT_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\s+(\d{4})\b",
    re.IGNORECASE,
)
_DATE_ISO_RE = re.compile(r"\b(\d{4})[\-/](\d{1,2})[\-/](\d{1,2})\b")


def _parse_date(text: str) -> str:
    """Return an ISO 8601 date (YYYY-MM-DD or YYYY-MM) or '' if no
    parseable date is found. Forgiving: scans the input for any of the
    common formats and returns the first match. Used for cover-page
    effective-date extraction."""
    if not text:
        return ""
    m = _DATE_ISO_RE.search(text)
    if m:
        y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
        return f"{y}-{mo}-{d}"
    m = _DATE_MONTH_YEAR_RE.search(text)
    if m:
        mo = _MONTHS.get(m.group(1).lower()[:3])
        if mo:
            return f"{m.group(3)}-{mo}-{m.group(2).zfill(2)}"
    m = _DATE_MONTH_YEAR_SHORT_RE.search(text)
    if m:
        mo = _MONTHS.get(m.group(1).lower()[:3])
        if mo:
            return f"{m.group(2)}-{mo}"
    return ""


def cover_metadata_from_analyze(result: dict[str, Any] | None) -> dict[str, str]:
    """Pure extraction: given a DI analyzeResult dict, return cover
    metadata. No I/O. Used by preanalyze.py (which already has the
    analyzeResult in hand) AND by cover_metadata_for_pdf below.

    Surfacing this without the blob-fetch wrapper means preanalyze can
    write cover_meta into output.json directly during phase_output,
    so the function-app skill chain at indexer time never has to
    backfill -- which on huge PDFs took 10-30 minutes per skill call
    and was blowing through the function-host timeout.
    """
    if not result:
        return {"document_revision": "", "effective_date": "", "document_number": ""}
    paragraphs = result.get("paragraphs") or []
    if not paragraphs:
        return {"document_revision": "", "effective_date": "", "document_number": ""}

    revision = ""
    effective_date = ""
    document_number = ""
    for para in paragraphs:
        on_cover = False
        for region in para.get("boundingRegions") or []:
            pn = region.get("pageNumber")
            if isinstance(pn, int) and pn in (1, 2, 3):
                on_cover = True
                break
        if not on_cover:
            continue
        content = (para.get("content") or "").strip()
        if not content:
            continue
        if not revision:
            m = _REVISION_RE.search(content)
            if m:
                revision = m.group(1).rstrip(".,;:")
        if not document_number:
            m = _DOC_NUMBER_RE.search(content)
            if m:
                document_number = m.group(1).rstrip(".,;:")
        if not effective_date:
            d = _parse_date(content)
            if d:
                effective_date = d
        if revision and effective_date and document_number:
            break

    return {
        "document_revision": revision,
        "effective_date": effective_date,
        "document_number": document_number,
    }


def cover_metadata_for_pdf(source_path: str) -> dict[str, str]:
    """Extract document-level metadata from the cover/title page of a
    PDF: revision identifier, effective date (ISO format), document
    number. Returns a dict with three string fields, each '' when not
    extractable.

    Why this matters: MANGOS technical manuals are revised over decades.
    The chatbot must be able to filter retrieval to the current revision
    of a manual; the LLM cannot tell a 1998 manual from a 2024 manual
    by inspecting page text alone. Surfacing these as filterable index
    fields lets the orchestrator say "only consider chunks where
    effective_date >= '2020-01'".

    Strategy: scan all paragraphs whose `boundingRegions[*].pageNumber`
    is in {1, 2, 3} (cover pages — many manuals push real metadata to
    page 2 or 3 with a logo/blank cover) and run revision/date/doc-
    number regexes over each. Earliest match wins for revision (cover
    is more authoritative than continuation pages); first-match for
    date and doc-number too.
    """
    if not source_path:
        return {"document_revision": "", "effective_date": "", "document_number": ""}
    result = _analysis_for(source_path)
    return cover_metadata_from_analyze(result)


def _ocr_min_confidence_for_pages(source_path: str, pages: list[int] | None) -> float | None:
    """Return the minimum DI per-word OCR confidence across all words on
    `pages`, or None when no confidence data is present (digital-text
    PDFs typically omit it; only OCR'd pages carry word-level confidence).

    Used to populate `ocr_min_confidence` on text records so a chatbot
    can caveat answers grounded in low-confidence OCR ("the manual *may*
    say 240V but the OCR confidence on this page is 0.62"). The
    chatbot orchestrator can also refuse-with-citation when the value
    drops below a configured threshold (e.g. 0.75).

    Implementation note: DI emits confidence at the word level. We take
    the min (worst case) rather than the mean so a single low-confidence
    word — which might be the answer ("240" vs "440") — surfaces.
    """
    if not source_path or not pages:
        return None
    page_set = {p for p in pages if isinstance(p, int)}
    if not page_set:
        return None
    # Use pre-indexed min-confidence-by-page (computed once per PDF)
    # instead of scanning all words on all pages every chunk. For a
    # 1000-page OCR'd PDF with ~1000 words/page, this was 1M+ word
    # accesses per chunk × hundreds of chunks per PDF.
    derived = _derived_for(source_path)
    min_by_page = derived["min_conf_by_page"]
    if not min_by_page:
        return None
    min_conf: float | None = None
    for pn in page_set:
        c = min_by_page.get(pn)
        if c is None:
            continue
        if min_conf is None or c < min_conf:
            min_conf = c
    return round(min_conf, 4) if min_conf is not None else None


def _footnotes_for_pages(source_path: str, pages: list[int] | None) -> list[str]:
    """Return all DI footnote paragraphs that fall on any of `pages`,
    preserving document order. Used to populate the `footnotes` collection
    field on each text record so a chatbot can surface IEEE / NEC / OSHA
    citations that live in footnotes alongside the body chunk that
    referenced them.

    DI tags footnote paragraphs with `role: "footnote"`. Each entry in
    the returned list is the verbatim footnote text — the citation UI
    (or the LLM) can decide whether to render them inline or as a
    collapsed list. We do NOT attempt marker→body linkage here (that
    requires parsing superscript markers in body paragraphs against
    leading numerals/glyphs in footnote paragraphs and is brittle); a
    follow-up pass can add `footnote_marker` / `parent_chunk_anchor`
    once we have a body-marker extractor.
    """
    if not source_path or not pages:
        return []
    page_set = {p for p in pages if isinstance(p, int)}
    if not page_set:
        return []
    # Use per-page paragraph index instead of scanning ALL paragraphs.
    # For huge PDFs, the chunk usually covers 1-3 pages; this drops the
    # per-call cost from O(total_paragraphs) to O(paragraphs_on_chunk_pages).
    derived = _derived_for(source_path)
    paras_by_page = derived["paragraphs_by_page"]
    out: list[str] = []
    seen: set[int] = set()  # paragraph identity (id() not stable across deserializes; use index)
    for pn in page_set:
        for idx, para in enumerate(paras_by_page.get(pn, [])):
            # Avoid emitting the same paragraph twice if it spans pages.
            key = id(para)
            if key in seen:
                continue
            role = (para.get("role") or "").lower()
            if role != "footnote":
                continue
            seen.add(key)
            content = (para.get("content") or "").strip()
            if content:
                # Cap each footnote at 500 chars so one long footnote
                # can't blow up the embedding string. Full content remains
                # in the raw DI cache for ops debugging.
                out.append(content[:500])
    return out


def _bbox_from_polygon(polygon: list[float]) -> tuple[float, float, float, float] | None:
    """Convert DI's 8-number polygon (x1,y1,...,x4,y4 in inches) to
    an axis-aligned (x, y, w, h) bbox. Returns None on malformed input."""
    if not polygon or len(polygon) < 8:
        return None
    try:
        xs = polygon[0::2]
        ys = polygon[1::2]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        return (float(x0), float(y0), float(x1 - x0), float(y1 - y0))
    except (TypeError, ValueError):
        return None


def _text_bbox_for_chunk(chunk_text: str, source_path: str) -> list[dict[str, Any]]:
    """Find DI paragraphs whose content appears in this chunk and return
    a per-page union bbox suitable for the front-end to render highlight
    rectangles on the rendered PDF page.

    Output shape: list of {page, x_in, y_in, w_in, h_in}, one entry per
    distinct page the chunk touches. Empty list if we can't resolve.

    Matching strategy (intentionally strict to avoid false positives on
    repeated boilerplate, TOC entries, and back-of-book index lines):
      1. Skip paragraphs shorter than 40 chars (TOC/index/footer artifacts).
      2. Use a 120-char head probe — long enough to be specific, short
         enough that paragraphs straddling a chunk boundary still match.
      3. For paragraphs longer than 200 chars, require a *second* window
         (chars 100-200) to also appear in the chunk. A common opening
         phrase ("For meter installations to residential...") will pass
         the head probe in TOC entries; only the real body paragraph
         will pass the second probe because TOC entries don't carry the
         continuation text.

    These three guards eliminate the multi-page-bbox false positives we
    saw in production where a single chunk's content matched paragraphs
    on the TOC page, the body page, and the appendix page simultaneously.
    """
    if not chunk_text or not source_path:
        return []
    # Use pre-normalized + pre-bbox-extracted paragraph candidates from
    # the per-PDF derived cache. Was calling _normalize_text on EVERY
    # paragraph EVERY chunk (5M regex calls per huge PDF), plus running
    # _bbox_from_polygon on every region. Now both are computed once
    # per PDF at derived-cache build time, and per-chunk work is just
    # substring matching against the cached normalized strings.
    candidates = _derived_for(source_path)["text_bbox_paragraphs"]
    if not candidates:
        return []

    chunk_norm = _normalize_text(chunk_text)
    if not chunk_norm:
        return []

    bboxes_by_page: dict[int, list[tuple[float, float, float, float]]] = {}
    for content, para_norm, page, bb in candidates:
        # Guard 1: minimum content length already enforced at derive time.
        # Guard 2: 120-char head probe.
        probe = para_norm[:120]
        if probe not in chunk_norm:
            continue
        # Guard 3: second-window verification for long paragraphs.
        if len(para_norm) >= 200:
            second = para_norm[100:200]
            if second and second not in chunk_norm:
                continue
        bboxes_by_page.setdefault(page, []).append(bb)

    out: list[dict[str, Any]] = []
    for page in sorted(bboxes_by_page):
        bxs = bboxes_by_page[page]
        x0 = min(b[0] for b in bxs)
        y0 = min(b[1] for b in bxs)
        x1 = max(b[0] + b[2] for b in bxs)
        y1 = max(b[1] + b[3] for b in bxs)
        out.append({
            "page": page,
            "x_in": round(x0, 3),
            "y_in": round(y0, 3),
            "w_in": round(x1 - x0, 3),
            "h_in": round(y1 - y0, 3),
        })
    return out


# Highlight-text construction is shared with diagram / table / summary
# skills via shared.text_utils.build_highlight_text — that helper does
# the same markdown/DI-marker stripping plus Unicode NFC normalize,
# soft-hyphen drop, smart-quote → ASCII, end-of-line hyphenation join,
# and control-character stripping. Importing instead of duplicating
# keeps the four record types' highlight contracts identical.


def _normalize_text(s: str) -> str:
    """Aggressive normalization for fuzzy content matching across the DI
    markdown output vs DI raw paragraph text. Strips markdown headers,
    list markers, HTML comments, and non-alphanumerics, then lowercases.
    This maximises the chance two logically-equal contents compare equal
    even when one is markdown-rendered and the other is paragraph-concat.
    """
    if not s:
        return ""
    # Drop HTML comments (PageBreak, PageNumber, PageFooter, etc.)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.DOTALL)
    # Drop markdown header/list markers at line starts
    s = re.sub(r"^\s*#{1,6}\s*", " ", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*[\-\*\+]\s+", " ", s, flags=re.MULTILINE)
    # Collapse to alphanumerics + single spaces, lowercased
    s = re.sub(r"[^A-Za-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _find_section_start_page(
    source_path: str,
    section_content: str,
    header_1: str = "",
    header_2: str = "",
    header_3: str = "",
) -> tuple[int | None, str]:
    """Look up this chunk's section in the DI cache and return its
    page_start plus a method tag describing which strategy hit:

      - "header_match" : exact match on h1+h2+h3, h1+h2, or h1 alone
      - "fuzzy_match"  : content substring after aggressive normalization
      - "missing"      : nothing matched

    The method tag is surfaced as the `page_resolution_method` field
    on each text record so UIs can decide whether to render a citation
    link with high confidence ("header_match") or de-emphasize it
    ("fuzzy_match" / "missing").
    """
    sections = _sections_for(source_path)
    if not sections:
        return None, "missing"

    def _first_page(matches: list[dict[str, Any]]) -> int | None:
        for s in matches:
            ps = s.get("page_start")
            if isinstance(ps, int):
                return ps
        return None

    # Headers were pre-normalized into `_h1n/_h2n/_h3n` inside
    # `_sections_for`. content was pre-normalized into `_content_norm`.
    # Per-chunk lookup is now dict-read instead of regex-substitute.
    # Audit 2026-05-12 measured 12M regex calls per huge PDF eliminated.
    #
    # Fallback: if a section dict was seeded directly into the cache
    # (e.g., by a test or by an alternate build path) and lacks the
    # `_h1n/...` fields, compute and stash them lazily so the section
    # works on first access AND subsequent accesses are cached.
    _norm_ws = re.compile(r"\s+")
    def _ensure_norm(s: dict[str, Any]) -> None:
        if "_h1n" not in s:
            s["_h1n"] = _norm_ws.sub(" ", (s.get("header_1") or "").strip())
            s["_h2n"] = _norm_ws.sub(" ", (s.get("header_2") or "").strip())
            s["_h3n"] = _norm_ws.sub(" ", (s.get("header_3") or "").strip())
            s["_content_norm"] = _normalize_text(s.get("content") or "")[:400]

    for s in sections:
        _ensure_norm(s)

    h1 = _norm_ws.sub(" ", (header_1 or "").strip())
    h2 = _norm_ws.sub(" ", (header_2 or "").strip())
    h3 = _norm_ws.sub(" ", (header_3 or "").strip())

    if h1 or h2 or h3:
        # Tier 1: full chain
        tier1 = [
            s for s in sections
            if s["_h1n"] == h1
            and s["_h2n"] == h2
            and s["_h3n"] == h3
        ]
        p = _first_page(tier1)
        if p is not None:
            return p, "header_match"
        # Tier 2: h1+h2 (chunks sometimes lose h3 at the split boundary)
        if h1 or h2:
            tier2 = [
                s for s in sections
                if s["_h1n"] == h1
                and s["_h2n"] == h2
            ]
            p = _first_page(tier2)
            if p is not None:
                return p, "header_match"
        # Tier 3: h1 alone
        if h1:
            tier3 = [s for s in sections if s["_h1n"] == h1]
            p = _first_page(tier3)
            if p is not None:
                return p, "header_match"

    # Tier 4: fuzzy content match. Capped at 100 sections to keep
    # per-chunk work bounded on very large manuals; headers should have
    # matched in tier 1-3 in the common case.
    probe = _normalize_text(section_content)[:400]
    if not probe:
        return None, "missing"
    probe_head = probe[:200]
    best: tuple[int, int] | None = None  # (overlap_len, page_start)
    for i, s in enumerate(sections):
        if i >= 100:
            break
        # _content_norm was stamped above by _ensure_norm if not present.
        content = s["_content_norm"]
        if not content:
            continue
        if probe_head and probe_head in content:
            ps = s.get("page_start")
            if isinstance(ps, int):
                overlap = min(len(content), len(probe))
                if best is None or overlap > best[0]:
                    best = (overlap, ps)
                # probe_head prefix match is strong; stop once we have one.
                break
        elif content[:200] and content[:200] in probe:
            ps = s.get("page_start")
            if isinstance(ps, int):
                overlap = min(len(content), len(probe))
                if best is None or overlap > best[0]:
                    best = (overlap, ps)
    if best:
        return best[1], "fuzzy_match"

    logging.info(
        "page_label: no DI-cache match for headers=(%r, %r, %r) in %s (have %d sections)",
        h1, h2, h3, source_path, len(sections),
    )
    return None, "missing"


def _is_roman(s: str) -> bool:
    return bool(re.fullmatch(r"[ivxlcdm]+", s, re.IGNORECASE))


def _candidate_lines(text: str):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    head = lines[:3]
    tail = lines[-3:]
    return head + tail


def _strip_di_markers(text: str) -> str:
    """Remove DI page markers before printed-label scanning so they don't
    pollute the heuristics."""
    if not text:
        return ""
    text = PAGE_NUMBER_MARKER_RE.sub("", text)
    text = PAGE_BREAK_MARKER_RE.sub("", text)
    return text


def _extract_label(text: str) -> str | None:
    if not text:
        return None
    text = _strip_di_markers(text)

    # Each tier validates with _is_valid_label() before returning. Without
    # this validation, PAGE_PREFIX_RE happily captures words like "refers"
    # from body text such as "this page refers to..." and emits them as
    # the printed_page_label.
    for line in _candidate_lines(text):
        m = PAGE_PREFIX_RE.search(line)
        if m and _is_valid_label(m.group(1)):
            return m.group(1)

    for line in _candidate_lines(text):
        m = TOC_LIKE_RE.search(line)
        if m:
            label = f"{m.group(1).upper()}-{m.group(2)}"
            if _is_valid_label(label):
                return label

    for line in _candidate_lines(text):
        m = SECTION_DASH_RE.search(line)
        if m and _is_valid_label(m.group(1)):
            return m.group(1)

    for line in _candidate_lines(text):
        m = DASH_NUM_RE.match(line)
        if m and _is_valid_label(m.group(1)):
            return m.group(1)

    for line in _candidate_lines(text):
        m = ROMAN_RE.fullmatch(line)
        if m and _is_roman(m.group(1)):
            return m.group(1).lower()

    return None


# ---------- page-span computation ----------


def _marker_timeline(section_content: str, section_start_page: int) -> list[tuple[int, int]]:
    """
    Walk the section content and return a sorted list of (offset, page_number).
    The first entry is always (0, section_start_page) so any chunk that
    sits before the first explicit marker still resolves to a real page.
    """
    timeline: list[tuple[int, int]] = [(0, section_start_page)]
    current_page = section_start_page

    # Combine both marker types in document order. PageNumber markers
    # carry the *printed* label (e.g. "18-33") which is not a physical
    # page number, so we only use them to double-check integer-style
    # labels (matches "3"). PageBreak is the reliable page advancer.
    events: list[tuple[int, str, int | None]] = []
    for m in PAGE_NUMBER_MARKER_RE.finditer(section_content or ""):
        label = (m.group(1) or "").strip()
        if label.isdigit():
            events.append((m.end(), "num", int(label)))
        # Non-numeric labels (e.g. "18-33") are informational only.
    for m in PAGE_BREAK_MARKER_RE.finditer(section_content or ""):
        events.append((m.end(), "break", None))
    events.sort(key=lambda e: e[0])

    for off, kind, val in events:
        if kind == "num" and val is not None:
            current_page = val
        elif kind == "break":
            current_page = current_page + 1
        timeline.append((off, current_page))

    return timeline


def _page_at_offset(timeline: list[tuple[int, int]], offset: int) -> int:
    """
    Binary-walk: return the page number active at `offset` (the page of the
    most recent marker whose end <= offset).
    """
    page = timeline[0][1]
    for off, pn in timeline:
        if off <= offset:
            page = pn
        else:
            break
    return page


def _last_page_segment(chunk: str) -> str:
    """Return the slice of `chunk` that follows the last DI page marker
    (PageNumber or PageBreak). Used for end-label extraction on
    multi-page chunks so we scan only the final physical page's text."""
    if not chunk:
        return ""
    last_end = -1
    for m in PAGE_NUMBER_MARKER_RE.finditer(chunk):
        last_end = max(last_end, m.end())
    for m in PAGE_BREAK_MARKER_RE.finditer(chunk):
        last_end = max(last_end, m.end())
    if last_end < 0:
        # No markers — fall back to the tail half so we still bias toward
        # the end of the chunk rather than scanning the whole thing.
        return chunk[len(chunk) // 2 :]
    return chunk[last_end:]


# Matches any run of trailing DI page markers (+ whitespace) at the end
# of a chunk. Stripped before offset computation so a chunk whose final
# content sits on page N but whose tail happens to include a PageBreak
# marker is not mis-attributed to page N+1.
TRAILING_MARKERS_RE = re.compile(
    r"(?:\s*<!--\s*(?:PageNumber\s*=\s*\"[^\"]*\"|PageBreak)\s*-->\s*)+\Z",
    re.IGNORECASE,
)


def _trim_trailing_markers(chunk: str) -> str:
    """Return chunk with any trailing DI page markers + whitespace removed."""
    if not chunk:
        return chunk
    return TRAILING_MARKERS_RE.sub("", chunk)


def _locate_chunk_in_section(chunk: str, section_content: str) -> int:
    """
    Find chunk start offset in section_content.

    In production SplitSkill emits chunks that are exact substrings of
    the section markdown, so `find(chunk)` is the fast path. The probe
    fallback handles edge cases where whitespace or marker rendering
    normalized slightly.
    """
    if not chunk or not section_content:
        return -1
    idx = section_content.find(chunk)
    if idx >= 0:
        return idx
    probe = _strip_di_markers(chunk)[:200].strip()
    if probe:
        idx = section_content.find(probe)
    return idx


def _pages_in_range(
    timeline: list[tuple[int, int]],
    start_off: int,
    end_off: int,
) -> list[int]:
    """All distinct pages active anywhere in [start_off, end_off]."""
    if end_off < start_off:
        end_off = start_off
    pages = {_page_at_offset(timeline, start_off)}
    for off, pn in timeline:
        if start_off <= off <= end_off:
            pages.add(pn)
        elif off > end_off:
            break
    return sorted(pages)


def compute_page_span(
    chunk: str,
    section_content: str,
    section_start_page: int | None,
) -> tuple[int | None, int | None, list[int]]:
    """
    Returns (start_page, end_page, pages_covered).

    pages_covered is the full sorted list of physical PDF pages the
    chunk touches — critical for citation / highlight UIs that need to
    resolve every page the chunk grounds.

    Falls back to (section_start_page, section_start_page, [section_start_page])
    if we cannot locate the chunk inside the section.
    """
    if section_start_page is None:
        # DI section pageNumber is unknown and we have no caller-supplied
        # cache lookup at this point. Caller (process_page_label) should
        # have already tried the DI-cache fallback; if we still got None,
        # there is nothing reliable to return.
        return None, None, []
    if not chunk:
        return section_start_page, section_start_page, [section_start_page]

    # Integer-style PageNumber markers inside the chunk (e.g. "3") are
    # used as a fallback when we cannot locate the chunk in section_content.
    # Printed labels like "18-33" are ignored here (they are surfaced as
    # printed_page_label instead).
    nums_in_chunk: list[int] = []
    for m in PAGE_NUMBER_MARKER_RE.finditer(chunk):
        lbl = (m.group(1) or "").strip()
        if lbl.isdigit():
            nums_in_chunk.append(int(lbl))
    breaks_in_chunk = list(PAGE_BREAK_MARKER_RE.finditer(chunk))

    if not section_content:
        if nums_in_chunk:
            lo = min([section_start_page] + nums_in_chunk)
            hi = max([section_start_page] + nums_in_chunk)
            return lo, hi, list(range(lo, hi + 1))
        if breaks_in_chunk:
            hi = section_start_page + len(breaks_in_chunk)
            return section_start_page, hi, list(range(section_start_page, hi + 1))
        return section_start_page, section_start_page, [section_start_page]

    timeline = _marker_timeline(section_content, section_start_page)
    chunk_start = _locate_chunk_in_section(chunk, section_content)
    if chunk_start < 0:
        if nums_in_chunk:
            lo = min(nums_in_chunk)
            hi = max(nums_in_chunk)
            return lo, hi, list(range(lo, hi + 1))
        return section_start_page, section_start_page, [section_start_page]

    # Use the *trimmed* chunk length so a trailing PageBreak marker
    # doesn't push chunk_end into the next page.
    effective_len = len(_trim_trailing_markers(chunk))
    chunk_end = chunk_start + effective_len
    start_page = _page_at_offset(timeline, chunk_start)
    end_page = _page_at_offset(timeline, chunk_end)
    if end_page < start_page:
        end_page = start_page
    pages = _pages_in_range(timeline, chunk_start, chunk_end)
    # Ensure start/end are present in the list.
    pages_set = set(pages)
    pages_set.add(start_page)
    pages_set.add(end_page)
    return start_page, end_page, sorted(pages_set)


# ---------- skill entry point ----------


def process_page_label(data: dict[str, Any]) -> dict[str, Any]:
    page_text = safe_str(data.get("page_text"))
    section_content = safe_str(data.get("section_content"))
    source_file = safe_str(data.get("source_file"))
    source_path = safe_str(data.get("source_path"))
    layout_ordinal = safe_int(data.get("layout_ordinal"), default=0)
    section_start_page = safe_int(data.get("physical_pdf_page"), default=None)
    h1_in = safe_str(data.get("header_1"))
    h2_in = safe_str(data.get("header_2"))
    h3_in = safe_str(data.get("header_3"))

    # Azure Search's DocumentIntelligenceLayoutSkill does not reliably
    # expose a per-section pageNumber in its markdown_document output, so
    # the `physical_pdf_page` input arrives as None for every chunk. When
    # that happens, recover the starting physical page by fetching the
    # DI cache blob (written by preanalyze.py) and matching this chunk's
    # section by header chain (primary) or content fuzzy match (fallback).
    if section_start_page is None:
        section_start_page, page_resolution_method = _find_section_start_page(
            source_path, section_content, h1_in, h2_in, h3_in,
        )
        # Diagnostic logging — surface what's happening per chunk so
        # operators can see WHY page resolution is failing in the wild.
        # Keep at INFO level so it shows up in Function App Log Stream
        # without needing Verbose.
        logging.info(
            "page_label: source=%s headers=(%r,%r,%r) -> start_page=%s method=%s",
            (source_path or "")[-60:], h1_in, h2_in, h3_in,
            section_start_page, page_resolution_method,
        )
    else:
        page_resolution_method = "di_input"

    start_page, end_page, pages_covered = compute_page_span(
        page_text, section_content, section_start_page
    )

    # text_bbox is computed from DI's paragraphs[].boundingRegions —
    # the most reliable per-paragraph page data DI emits. It is used in
    # three ways below, in order of authority:
    #   (a) as the primary fallback when section resolution returned None
    #       (paragraph_bbox method);
    #   (b) as a cross-validator when section resolution returned a page
    #       that disagrees with the bbox (bbox_corrected method) — this
    #       catches the case where DI groups paragraphs from earlier pages
    #       into a section and our index records a too-early page_start;
    #   (c) for the final text_bbox field surfaced to the front-end.
    text_bbox_list = _text_bbox_for_chunk(page_text, source_path)

    # Build a map of {page -> total bbox area} for cross-validation. The
    # page with the largest area is the page that owns the bulk of the
    # chunk's content; ancillary entries (chunk content also appearing
    # on a TOC page or an appendix) carry tiny bboxes and lose the tie.
    bbox_pages_area: dict[int, float] = {}
    for b in text_bbox_list:
        pg = b.get("page")
        w = b.get("w_in") or 0.0
        h = b.get("h_in") or 0.0
        if isinstance(pg, int):
            bbox_pages_area[pg] = bbox_pages_area.get(pg, 0.0) + float(w) * float(h)

    if start_page is None and bbox_pages_area:
        # Path (a): section resolution failed entirely; bbox is the only
        # signal. Use the page with the largest content area as the
        # canonical page; surface the full bbox span for end_page.
        primary = max(bbox_pages_area, key=bbox_pages_area.get)
        bbox_pages = sorted(bbox_pages_area.keys())
        start_page = primary
        end_page = max(bbox_pages)
        if end_page < start_page:
            end_page = start_page
        pages_covered = bbox_pages
        page_resolution_method = "paragraph_bbox"

    elif start_page is not None and bbox_pages_area:
        # Path (b): section resolution gave us an answer; cross-check it
        # against the bbox. If our answer is not among the bbox pages,
        # the section index lied (typically because DI grouped earlier-
        # page paragraphs into the section). Trust the bbox.
        if start_page not in bbox_pages_area:
            primary = max(bbox_pages_area, key=bbox_pages_area.get)
            bbox_pages = sorted(bbox_pages_area.keys())
            start_page = primary
            end_page = primary
            pages_covered = [primary]
            page_resolution_method = "bbox_corrected"

    # Final fallback (Path c): cache fetch failed entirely (e.g. function
    # app can't reach blob storage, or DI cache file is malformed). Try
    # to recover a physical page from the chunk's own DI markdown markers:
    # `<!-- PageNumber="N" -->` and `<!-- PageBreak -->` carry physical-
    # page transitions inline in the chunk text. Even when the cache is
    # totally unreachable, these inline markers give us SOMETHING.
    #
    # We use the first integer marker found in either the chunk OR the
    # section content. Printed labels like "18-33" are skipped — only
    # integer values map cleanly to physical pages.
    if start_page is None:
        recovered_page: int | None = None
        for source_text in (page_text or "", section_content or ""):
            for m in PAGE_NUMBER_MARKER_RE.finditer(source_text):
                lbl = (m.group(1) or "").strip()
                if lbl.isdigit():
                    recovered_page = int(lbl)
                    break
            if recovered_page is not None:
                break
        if recovered_page is not None:
            start_page = recovered_page
            end_page = recovered_page
            pages_covered = [recovered_page]
            page_resolution_method = "marker_inline"
            logging.info(
                "page_label: cache fetch failed for source=%s headers=(%r,%r,%r); "
                "recovered page %d from inline marker",
                (source_path or "")[-60:], h1_in, h2_in, h3_in, recovered_page,
            )
        else:
            page_resolution_method = "missing"
            logging.warning(
                "page_label: ALL fallbacks failed for source=%s headers=(%r,%r,%r) "
                "section_content_len=%d page_text_len=%d bbox_pages=%d",
                (source_path or "")[-60:], h1_in, h2_in, h3_in,
                len(section_content or ""), len(page_text or ""),
                len(bbox_pages_area),
            )

    # Prefer the explicit `<!-- PageNumber="..." -->` marker from DI when
    # present in the chunk — for technical manuals it holds the printed
    # label the reader would recognise (e.g. "18-33", "iv", "A-12").
    # If no real label can be extracted, fall back to the physical page
    # number string. This is a UX requirement from the citation UI:
    # the field must never be blank for the user, even when the source
    # page literally doesn't print a label (cover, copyright, full-bleed
    # figures). The `printed_page_label_is_synthetic` flag below tells
    # downstream consumers when the label was real vs. synthesised so
    # they can mark such citations as "approximate" if they want to.
    label = ""
    label_is_synthetic = False
    for m in PAGE_NUMBER_MARKER_RE.finditer(page_text or ""):
        candidate = (m.group(1) or "").strip()
        if candidate:
            label = candidate
            break
    if not label:
        label = _extract_label(page_text) or ""
    # DI cache fallback: scan pageNumber/pageFooter/pageHeader paragraphs
    # on this physical page. Chapter-prefixed labels like "5-7" live in
    # page footers, not in the chunk body, so the chunk-text heuristics
    # above never see them. _printed_label_for_page reaches the DI cache
    # and pulls the canonical label for this physical page.
    if not label:
        label = _printed_label_for_page(source_path, start_page) or ""
    if not label and start_page is not None:
        label = str(start_page)
        label_is_synthetic = True

    # End-label extraction: for multi-page chunks, first try the last DI
    # PageNumber marker in the chunk (which carries the printed label).
    # Fall back to heuristic parsing of the final segment if there is no
    # explicit marker. Same fallback rule as above: synthesize from the
    # physical page when no real label can be found.
    end_label = ""
    if page_text and end_page is not None and start_page is not None and end_page > start_page:
        last_marker = ""
        for m in PAGE_NUMBER_MARKER_RE.finditer(page_text):
            cand = (m.group(1) or "").strip()
            if cand:
                last_marker = cand
        end_label = last_marker or (_extract_label(_last_page_segment(page_text)) or "")
        # DI cache fallback for the end label too — same rationale as
        # the start-label case: chapter-prefixed labels live in footers.
        if not end_label:
            end_label = _printed_label_for_page(source_path, end_page) or ""
    if not end_label:
        if start_page == end_page:
            # Single-page chunk: end mirrors start.
            end_label = label
        elif end_page is not None:
            # Multi-page chunk with no extracted end-label: synthesize.
            end_label = str(end_page)
            label_is_synthetic = True

    # Extract figure and table references from the text chunk so text
    # records carry these anchors as filterable fields and the semantic
    # ranker can boost on them. `figure_ref` / `table_ref` are kept as
    # comma-joined strings for backwards compatibility; the new
    # `figures_referenced` / `tables_referenced` collections are the
    # filterable form (use $filter=figures_referenced/any(f: f eq '...')).
    #
    # The regex permits internal dots/dashes (so "Figure 18.117" and
    # "Table A-1" both match) but a captured ID can also end on a
    # sentence-terminating period if the reference appears at end of
    # sentence. We strip trailing punctuation so "Table A-1." normalizes
    # to "Table A-1".
    def _clean_ref(s: str) -> str:
        return s.rstrip(".-,;:")

    fig_refs = sorted(
        set(
            f"{m.group(1).title()} {_clean_ref(m.group(2))}"
            for m in FIGURE_REF_RE.finditer(page_text)
            if _clean_ref(m.group(2))
        )
    )
    figure_ref = ", ".join(fig_refs) if fig_refs else ""

    # Cross-record join key. The frontend / chatbot can issue one filter
    # query that finds both the diagram record AND every text record
    # mentioning the same figure:
    #   figures_referenced_normalized/any(f: f eq '18117')
    # Both record types populate this field with the same normalization
    # (lowercased, separators stripped) so typography variations like
    # "Figure 18.117" vs "Fig 18-117" join cleanly. Imported lazily to
    # avoid circular import with diagram.py.
    from .diagram import normalize_figure_ref
    fig_refs_normalized = sorted({
        n for n in (normalize_figure_ref(f) for f in fig_refs) if n
    })

    tbl_refs = sorted(
        set(
            f"Table {_clean_ref(m.group(2))}"
            for m in TABLE_REF_RE.finditer(page_text)
            if _clean_ref(m.group(2))
        )
    )
    table_ref = ", ".join(tbl_refs) if tbl_refs else ""

    # Section + page cross-references. Parallel to figures_referenced /
    # tables_referenced — these collections let a chatbot answering
    # "what does Section 4.2 say?" or "what's on page 18-25?" filter
    # text records by `sections_referenced/any(s: s eq '4.2')` instead
    # of full-text searching for the phrase "Section 4.2".
    section_refs = sorted({
        _clean_ref(m.group(1))
        for m in SECTION_REF_RE.finditer(page_text or "")
        if _clean_ref(m.group(1))
    })
    page_refs = sorted({
        _clean_ref(m.group(1))
        for m in PAGE_REF_RE.finditer(page_text or "")
        if _clean_ref(m.group(1))
    })

    # Highlight + bbox + total-pages: new fields to support precise
    # client-side highlighting in the citation UI. text_bbox_list was
    # already computed earlier so we could use it as a page-resolution
    # fallback; reuse it here for serialization.
    highlight_text = build_highlight_text(page_text)

    # Whole-page-bbox fallback. The strict paragraph matcher can return
    # empty for chunks with fragmentary content (form data, GIS layer
    # names, single-word lists) even when we KNOW the physical page. To
    # ensure every text record with a known page gets SOME bbox, emit a
    # full-page rectangle as a last resort. The frontend can render a
    # faint full-page highlight in this case rather than no highlight at
    # all. Method tag stays "header_match"/etc. (the page resolution was
    # the real signal); callers can detect "is this a precise bbox or a
    # whole-page fallback" by checking if the bbox covers the full page.
    if not text_bbox_list and start_page is not None:
        text_bbox_list = [_whole_page_bbox(source_path, start_page)]

    text_bbox_json = json.dumps(text_bbox_list, separators=(",", ":")) if text_bbox_list else ""
    pdf_total_pages = _pdf_total_pages_for(source_path)

    # Safety callouts (WARNING / DANGER / CAUTION / NOTICE / NOTE).
    # Extract the deduped keyword list so the index can filter on
    # `callouts/any(c: c eq 'DANGER')` and the UI can render a badge.
    # Also surface a boolean for the common case (any callout present).
    # Note: imported lazily to avoid circular import with semantic.py.
    from .semantic import extract_callout_keywords
    callout_keywords = extract_callout_keywords(page_text)

    # Footnotes that sit on this chunk's physical pages. DI tags them
    # with role=footnote; we surface them as a collection field so a
    # chatbot can answer "what does footnote 3 in the meter section say"
    # and the UI can render them as collapsed citations beneath the
    # main body. The body chunk itself still contains the footnote text
    # verbatim (DI emits footnotes as ordinary paragraphs in document
    # order), so this is purely a retrieval/display assist — it doesn't
    # duplicate searchable content.
    footnotes_list = _footnotes_for_pages(source_path, pages_covered)

    # OCR confidence (None for digital-text pages, 0.0-1.0 for OCR'd
    # pages). Surfaced so the chatbot can caveat or down-rank answers
    # grounded in low-confidence scanned content. We take the per-word
    # minimum across all words on the chunk's pages — worst-case is
    # what matters when a single mis-OCR'd digit ("240" vs "440")
    # could change the meaning of a safety answer.
    ocr_min_conf = _ocr_min_confidence_for_pages(source_path, pages_covered)

    # Document-level metadata mined from the cover page. Propagated to
    # every text record so retrieval can filter by current revision /
    # effective date — critical for safety manuals that are rev'd
    # repeatedly and where stale guidance can be dangerous.
    # NOTE: read from the per-PDF derived cache instead of re-scanning
    # the full paragraphs[] array on every chunk. The previous direct
    # cover_metadata_for_pdf(source_path) call was doing 10K+ paragraph
    # iterations per chunk on huge PDFs.
    cover_meta = _derived_for(source_path)["cover_meta"]

    # TOC / list-of-figures detection. UIs that want clean retrieval
    # filter on processing_status eq 'ok' and never see TOC fragments.
    status = "toc_like" if _is_toc_like(page_text) else "ok"

    # Glossary detection. Stamps record_subtype="glossary" on definition /
    # acronym chunks so a chatbot can prefer-rank them for "what does X
    # mean?" queries. Header-match is the primary signal; body-pattern
    # match catches glossary content under non-obvious headers.
    record_subtype = "glossary" if _is_glossary_chunk(page_text, h1_in, h2_in, h3_in) else ""

    # Tier-5 ops fields: equipment_id collection (for exact-match lookup
    # by part / model number), language code (future Spanish/French
    # manuals will flag themselves), composite quality score (tie-breaker
    # when the semantic ranker can't separate two hits).
    equipment_ids = _extract_equipment_ids(page_text)
    language = _detect_language(page_text)
    quality_score = _compute_quality_score(
        page_resolution_method=page_resolution_method,
        chunk_len=len(page_text or ""),
        has_headers=bool(h1_in or h2_in or h3_in),
        is_toc_like=(status == "toc_like"),
        has_callouts=bool(callout_keywords),
        has_figure_or_table_ref=bool(fig_refs or tbl_refs),
    )

    return {
        "chunk_id": text_chunk_id(source_path, source_file, layout_ordinal, page_text),
        "parent_id": parent_id_for(source_path, source_file),
        "record_type": "text",
        "printed_page_label": label,
        "printed_page_label_end": end_label,
        "printed_page_label_is_synthetic": label_is_synthetic,
        "physical_pdf_page": start_page,
        "physical_pdf_page_end": end_page,
        "physical_pdf_pages": pages_covered,
        "figure_ref": figure_ref,
        "figures_referenced": fig_refs,
        "figures_referenced_normalized": fig_refs_normalized,
        "table_ref": table_ref,
        "tables_referenced": tbl_refs,
        "sections_referenced": section_refs,
        "pages_referenced": page_refs,
        "highlight_text": highlight_text,
        "text_bbox": text_bbox_json,
        "pdf_total_pages": pdf_total_pages,
        "page_resolution_method": page_resolution_method,
        "callouts": callout_keywords,
        "safety_callout": bool(callout_keywords),
        "footnotes": footnotes_list,
        "ocr_min_confidence": ocr_min_conf,
        "document_revision": cover_meta["document_revision"],
        "effective_date": cover_meta["effective_date"],
        "document_number": cover_meta["document_number"],
        "record_subtype": record_subtype,
        # Ops fields — let the orchestrator do prompt-budget math, let
        # ops queries find stale rows, let the chatbot prefer rows that
        # match the current embedding model.
        "chunk_token_count": _approx_token_count(page_text),
        "embedding_version": _embedding_version(),
        "last_indexed_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "equipment_ids": equipment_ids,
        "language": language,
        "chunk_quality_score": quality_score,
        "processing_status": status,
        "skill_version": SKILL_VERSION,
    }
