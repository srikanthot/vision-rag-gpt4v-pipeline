"""
analyze-diagram (per-figure)

Inputs come from process-document's enriched_figures:
  - image_b64 (already cropped to the figure region)
  - figure_id, page_number, caption, bbox
  - header_1/2/3, surrounding_context
  - source_file, source_path, parent_id

The vision prompt is enriched with section path, page, caption, surrounding
text, and any figure refs we can recover from the surrounding text.

Hash cache: before calling vision, we look up an existing index document
with the same parent_id + image_hash. On a hit we copy the previous
description and skip the vision call entirely.
"""

import base64
import hashlib
import json
import logging
import re
from typing import Any

from .aoai import get_client, vision_deployment
from .di_client import fetch_cached_crop, fetch_precomputed_vision
from .ids import (
    SKILL_VERSION,
    diagram_chunk_id,
    safe_int,
    safe_str,
)
from .search_cache import lookup_existing_by_hash, lookup_existing_by_phash
from .text_utils import build_highlight_text

VALID_CATEGORIES = {
    "circuit_diagram",
    "wiring_diagram",
    "schematic",
    "line_diagram",
    "block_diagram",
    "pid_diagram",
    "flow_diagram",
    "control_logic",
    "exploded_view",
    "parts_list_diagram",
    "nameplate",
    "equipment_photo",
    "table_image",
    "decorative",
    "unknown",
}

USEFUL_CATEGORIES = {
    "circuit_diagram",
    "wiring_diagram",
    "schematic",
    "line_diagram",
    "block_diagram",
    "pid_diagram",
    "flow_diagram",
    "control_logic",
    "exploded_view",
    "parts_list_diagram",
    "nameplate",
    "equipment_photo",
}

MIN_CROP_BYTES = 10_000  # must match preanalyze.py triage threshold

SYSTEM_PROMPT = """You are a technical-manual diagram analyst.

Return STRICT JSON with these keys:
  category:    one of [circuit_diagram, wiring_diagram, schematic, line_diagram, block_diagram, pid_diagram, flow_diagram, control_logic, exploded_view, parts_list_diagram, nameplate, equipment_photo, decorative, unknown]
  is_useful:   boolean. true unless category is decorative/unknown.
  figure_ref:  e.g. "Figure 4-2", "Fig. 12", or "" if none visible.
  description: dense, retrieval-friendly description (3-8 sentences).
               For diagrams, name components, labels, connections, units, and
               what the diagram is showing. For nameplates, transcribe key
               fields. If any text or value is unclear, say so explicitly.
               Do not guess.
  ocr_text:    transcribe ALL visible text labels, part numbers, values,
               wire tags, terminal IDs, model numbers, and callout numbers
               found in the image. Preserve the original text exactly.
               Separate items with " | ". If no readable text, return "".

Return ONLY the JSON object. No markdown, no commentary."""


FIGURE_REF_RE = re.compile(
    r"\b(Figure|Fig\.?)\s*[\-:]?\s*([A-Z]{0,3}[\-\.]?\d[\w\-\.]{0,8})",
    re.IGNORECASE,
)


def normalize_figure_ref(s: str) -> str:
    """Normalize a figure reference for cross-record joins.

    "Figure 18.117"  → "18117"
    "Fig 18-117"     → "18117"
    "Figure A-1"     → "a1"
    "FIG. 4.2"  → "42"
    ""               → ""

    Strips the Figure/Fig prefix and all separators (dots, dashes,
    whitespace, NBSP, em-dash). The result is what frontend / Search
    queries should use when joining text-record `figures_referenced_normalized`
    to diagram-record `figures_referenced_normalized`. The original
    `figure_ref` ("Figure 18.117") is preserved separately for display.
    """
    if not s:
        return ""
    # Drop the prefix; we only want the ID portion. Be tolerant of
    # NBSP, em-dash, and double whitespace introduced by DI typography.
    cleaned = re.sub(r"\b(figure|fig)\.?\s*[\-:]?\s*", "", s, flags=re.IGNORECASE)
    # Strip any remaining non-alphanumerics — separators are noise for joins.
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", cleaned)
    return cleaned.lower()


def _image_hash(image_b64: str, raw: bytes | None = None) -> str:
    """SHA-256 of the decoded image bytes. Pass `raw` if already decoded
    (e.g. from _decode_b64_once below) to avoid a duplicate base64 decode."""
    if not image_b64:
        return "noimage"
    try:
        if raw is None:
            raw = base64.b64decode(image_b64)
        return hashlib.sha256(raw).hexdigest()
    except Exception:
        return hashlib.sha256(image_b64.encode("utf-8")).hexdigest()


def _decode_b64_once(image_b64: str) -> bytes | None:
    """Decode base64 once, return None on failure. Used to share the
    decoded bytes across _image_hash, _image_phash, and triage logic
    that previously each decoded the same string independently. For a
    PDF with 1500 figures × 5MB base64 each, that's 22.5GB of decode
    work saved per indexer run."""
    if not image_b64:
        return None
    try:
        return base64.b64decode(image_b64)
    except Exception:
        return None


# Skip phash entirely for crops larger than this (raw decoded bytes).
# PIL.Image.open decompresses the full bitmap into memory; a 10MB PNG can
# be 200+ MB uncompressed for a 50-megapixel image. With dop=6 across the
# function app's worker processes, several concurrent calls processing
# large images simultaneously have caused worker OOM crashes (HTTP 500
# returned to indexer with no Python traceback, because the OS killed the
# worker process). Skipping phash on huge images sacrifices cross-PDF
# perceptual dedup for those specific figures, but keeps the worker alive.
# 8 MB raw is well above any realistic MANGOS figure crop -- crops in their
# corpus are typically 100KB-2MB.
_PHASH_MAX_BYTES = 8 * 1024 * 1024  # 8 MB

# Maximum pixels we'll allow PIL to decompress. PIL's default
# MAX_IMAGE_PIXELS is ~90 megapixels and only emits a warning above that
# (Image.DecompressionBombWarning). We override to RAISE
# DecompressionBombError above 30MP, which is the upper bound for any
# legitimate PDF figure crop. Anything bigger is either a bug in the
# rendering or an adversarial input -- abort cleanly instead of OOM.
_PHASH_MAX_PIXELS = 30_000_000  # 30 megapixels


def _image_phash(image_b64: str, raw: bytes | None = None) -> str:
    """Perceptual hash (dHash variant) over a grayscale 9x8 thumbnail.

    Pass `raw` if already decoded (e.g. from _decode_b64_once) to skip
    a redundant base64 decode. Same return semantics as before.

    Memory-safety hardening (2026-05-13):
      - Skip entirely if raw decoded bytes > _PHASH_MAX_BYTES
      - Cap PIL's MAX_IMAGE_PIXELS at _PHASH_MAX_PIXELS (raises on overshoot)
      - Catch MemoryError specifically and log loudly
    Without this, large embedded images (50+ megapixel rasters in some
    PDFs) caused worker process OOM crashes -- HTTP 500 to indexer with
    no Python traceback.
    """
    if not image_b64 and raw is None:
        return ""
    if raw is None:
        try:
            raw = base64.b64decode(image_b64)
        except Exception:
            return ""

    # Hard size guard BEFORE PIL touches the bytes. PIL decompresses
    # into memory eagerly; oversized images can blow the worker.
    if len(raw) > _PHASH_MAX_BYTES:
        logging.info(
            "phash: skipping oversized image (%d bytes > %d limit)",
            len(raw), _PHASH_MAX_BYTES,
        )
        return ""

    try:
        from io import BytesIO
        from PIL import Image
        # Tighten PIL's decompression-bomb defense locally for this call.
        # PIL's default raises a warning at 90MP; we raise a hard error
        # at 30MP -- well above any real PDF figure crop.
        _prev_max = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = _PHASH_MAX_PIXELS
        try:
            # Image.open is lazy; the file handle stays open until close().
            with Image.open(BytesIO(raw)) as src:
                # Force size validation up front; raises DecompressionBombError
                # if pixel count exceeds MAX_IMAGE_PIXELS.
                src.load()
                img = src.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        finally:
            Image.MAX_IMAGE_PIXELS = _prev_max

        pixels = list(img.getdata())  # 72 grayscale ints
        bits = []
        for row in range(8):
            for col in range(8):
                left = pixels[row * 9 + col]
                right = pixels[row * 9 + col + 1]
                bits.append(1 if left > right else 0)
        # Pack 64 bits into a 16-hex-char string.
        n = 0
        for b in bits:
            n = (n << 1) | b
        return f"{n:016x}"
    except MemoryError as exc:
        # The most common silent worker-killer. Log loudly so we have a
        # trace before the worker potentially dies on the next allocation.
        logging.error("phash: MemoryError on %d-byte image: %s", len(raw), exc)
        return ""
    except Exception as exc:
        # Includes PIL.Image.DecompressionBombError -- which is the new
        # safe failure for oversized images that previously crashed the worker.
        logging.warning("phash failed: %s: %s", type(exc).__name__, exc)
        return ""


def phash_distance(a: str, b: str) -> int:
    """Hamming distance between two 16-hex-char phashes. Returns 64 (max)
    when either is empty / malformed. Threshold of <=8 is a typical
    'visually identical' band for dHash; <=16 catches mild compression
    / DPI variants."""
    if not a or not b or len(a) != len(b):
        return 64
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 64


# Category-specific extraction hints. We don't run a separate
# classifier call (that would double cost); instead we use caption
# and surrounding-text keywords to guess the likely category and
# include tailored extraction instructions in the user prompt. The
# model still classifies definitively in its JSON response, but the
# tailored hints meaningfully improve description quality on the
# common figure types in technical manuals.
_CATEGORY_HINTS = {
    "schematic_wiring": (
        "Likely a schematic / wiring diagram. In addition to the general "
        "description, transcribe EVERY visible reference designator (R1, "
        "C2, T3, F1, K1, J1, etc.) in declaration order. Note all wire "
        "tags, terminal IDs, and pin numbers. List signal flow between "
        "labeled terminals (e.g. 'J1 pin 3 -> TB-2 terminal A'). Identify "
        "voltage levels, currents, and component values where shown."
    ),
    "nameplate": (
        "Likely an equipment nameplate or label plate. Extract as STRUCTURED "
        "fields in the description: Manufacturer, Model, Serial, "
        "Voltage rating, Current rating, Power rating, Frequency, Phase, "
        "Insulation class, Enclosure rating, Date/year, Standards (UL/CSA/IEC). "
        "If a field is illegible say so explicitly — do not guess."
    ),
    "block_flow": (
        "Likely a block / flow / P&ID / control-logic diagram. List every "
        "labeled block in document order, then describe the connections "
        "(arrows / signal lines) between them. For P&ID, capture loop tags "
        "(e.g. FT-101, PV-204) and instrument types. Note any setpoints "
        "or threshold values shown."
    ),
    "parts_exploded": (
        "Likely an exploded view / parts list. Transcribe every callout "
        "number and the part it references. Note assembly order if "
        "implied by the numbering. Capture any torque / fastener specs "
        "shown alongside the callouts."
    ),
    "default": (
        "Describe what the figure shows in technical detail. Name "
        "components, labels, units, and connections. If text is "
        "unclear, say so — do not guess values."
    ),
}


def _guess_category_from_context(caption: str, surrounding: str) -> str:
    """Cheap pre-classification using caption / surrounding-text keywords.
    Returns one of `_CATEGORY_HINTS` keys. The model still makes the
    final classification call in its JSON response — this just adds
    tailored extraction guidance to the prompt."""
    text = (caption + " " + surrounding).lower()
    if any(k in text for k in (
        "wiring diagram", "schematic", "single-line", "single line",
        "one-line", "one line", "circuit",
    )):
        return "schematic_wiring"
    if any(k in text for k in (
        "nameplate", "name plate", "rating plate", "label plate", "data plate",
    )):
        return "nameplate"
    if any(k in text for k in (
        "block diagram", "p&id", "p & id", "flow diagram", "control logic",
        "logic diagram", "process flow",
    )):
        return "block_flow"
    if any(k in text for k in (
        "exploded view", "parts list", "parts diagram", "assembly drawing",
        "callouts", "call-outs",
    )):
        return "parts_exploded"
    return "default"


def _build_user_text(data: dict[str, Any]) -> str:
    source_file = safe_str(data.get("source_file"))
    h1 = safe_str(data.get("header_1"))
    h2 = safe_str(data.get("header_2"))
    h3 = safe_str(data.get("header_3"))
    header_path = " > ".join([h for h in [h1, h2, h3] if h])
    page = safe_str(data.get("page_number"))
    caption = safe_str(data.get("caption"))
    surrounding = safe_str(data.get("surrounding_context"))

    refs = ", ".join(
        sorted(set(f"{m.group(1).title()} {m.group(2)}" for m in FIGURE_REF_RE.finditer(surrounding)))
    )

    surrounding_safe = surrounding[:1500].replace('"', "'")

    # Pick category-specific extraction guidance based on caption /
    # surrounding text keywords. Falls back to the default prompt for
    # photos and figures we can't pre-classify.
    category_guess = _guess_category_from_context(caption, surrounding)
    category_hint = _CATEGORY_HINTS[category_guess]

    return (
        f"You are analyzing a figure from technical manual \"{source_file}\".\n"
        f"Section: {header_path or '(unknown)'}\n"
        f"Page: {page or '(unknown)'}\n"
        f"Caption (from layout): {caption or '(none)'}\n"
        f"Body text references this figure as: {refs or '(none)'}\n"
        f"Surrounding text: \"{surrounding_safe}\"\n\n"
        f"Category-specific guidance: {category_hint}\n\n"
        f"If this is a technical diagram, describe it in full detail.\n"
        f"If any text/value is unclear, say so explicitly. Do not guess.\n"
        f"If decorative/logo/photo, return category=decorative and is_useful=false."
    )


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def _call_vision(image_b64: str, user_text: str) -> dict[str, Any]:
    client = get_client()
    user_content = [
        {"type": "text", "text": user_text},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
        },
    ]
    resp = client.chat.completions.create(
        model=vision_deployment(),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.0,
        max_tokens=1500,
        response_format={"type": "json_object"},
        # Explicit per-call timeout. Vision calls on stuck Gov Cloud
        # sockets can hang up to the SDK default 600s, blowing past the
        # 230s skill timeout and tying up function workers.
        timeout=60.0,
    )
    return _extract_json(resp.choices[0].message.content or "{}")


def process_diagram(data: dict[str, Any]) -> dict[str, Any]:
    image_b64 = safe_str(data.get("image_b64"))
    figure_id = safe_str(data.get("figure_id"))
    page_number = safe_int(data.get("page_number"), default=None)
    caption = safe_str(data.get("caption"))
    h1 = safe_str(data.get("header_1"))
    h2 = safe_str(data.get("header_2"))
    h3 = safe_str(data.get("header_3"))
    surrounding = safe_str(data.get("surrounding_context"))
    source_file = safe_str(data.get("source_file"))
    source_path = safe_str(data.get("source_path"))
    parent_id = safe_str(data.get("parent_id"))
    pdf_total_pages = safe_int(data.get("pdf_total_pages"), default=None)
    bbox = data.get("bbox") or {}
    # Serialize as a single-element list of {page, x_in, y_in, w_in, h_in}
    # so the citation contract matches text_bbox (list of per-page entries)
    # and table_bbox (list of per-page entries). Frontend can render
    # highlights uniformly across record types: parse JSON → iterate the
    # array → for each entry draw a rect on entry.page.
    bbox_json = json.dumps([bbox], separators=(",", ":")) if bbox else ""

    # Decode base64 ONCE and share across both hash functions. Without
    # this both functions independently decoded the same 5MB string,
    # tripling decode CPU for every figure. For a PDF with 1500 figures,
    # this saves ~22GB of decode work.
    #
    # phash is computed LAZILY (filled with "" upfront, populated only
    # when needed). _image_phash runs PIL Image.open + LANCZOS resize
    # (~80-200ms per 5MB image). The precomputed-vision fast path needs
    # img_hash for the chunk_id, but does NOT need phash. Deferring
    # phash to the slow path (sha256 cache miss AND precomputed-vision
    # miss) saves ~30-100s per PDF on precomputed-heavy workloads.
    raw_bytes = _decode_b64_once(image_b64)
    img_hash = _image_hash(image_b64, raw=raw_bytes)
    img_phash = ""  # lazily computed if/when we reach the phash cache lookup
    chunk_id = diagram_chunk_id(source_path, source_file, img_hash)

    # Diagrams always live on a single physical page, so physical_pdf_pages
    # is a single-element list. Kept for parity with text/table records so
    # the UI can use the same `physical_pdf_pages/any(...)` filter pattern
    # across all record types.
    physical_pdf_pages = [page_number] if isinstance(page_number, int) else []

    # Printed page label: diagrams don't run through extract-page-label,
    # so we synthesize from the physical page number. Same UX rule as
    # text/table: never blank when we know the page.
    printed_label = str(page_number) if isinstance(page_number, int) else ""

    base_record = {
        "chunk_id": chunk_id,
        "parent_id": parent_id,
        "record_type": "diagram",
        "figure_id": figure_id,
        "figure_bbox": bbox_json,
        "image_hash": img_hash,
        # Perceptual hash — robust to PyMuPDF rendering non-determinism
        # across font subsetting / antialias / DPI tile cache. Used for
        # cross-PDF dedup (same OEM nameplate in 50 manuals = 1 vision
        # call instead of 50) and as a secondary intra-PDF dedup signal
        # when SHA-256 misses a near-duplicate render.
        "image_phash": img_phash,
        "physical_pdf_page": page_number,
        "physical_pdf_page_end": page_number,
        "physical_pdf_pages": physical_pdf_pages,
        "printed_page_label": printed_label,
        "printed_page_label_end": printed_label,
        "printed_page_label_is_synthetic": bool(printed_label),
        "pdf_total_pages": pdf_total_pages,
        # DI gave us the page number directly via boundingRegions, so we
        # treat this as the highest-confidence resolution path. The UI
        # can use `page_resolution_method == "di_input"` as a green flag
        # for citation links across record types.
        "page_resolution_method": "di_input" if isinstance(page_number, int) else "missing",
        "header_1": h1,
        "header_2": h2,
        "header_3": h3,
        "surrounding_context": surrounding,
        "skill_version": SKILL_VERSION,
    }

    # Cover metadata. Read from input data only -- process-document /
    # preanalyze always supply these fields (empty when not extractable).
    # The previous "fallback to cover_metadata_for_pdf" branch was
    # burning 14-22 min per call on PDFs without cover metadata,
    # blowing past the 230s Azure WebApi skill timeout.
    cover_meta = {
        "document_revision": safe_str(data.get("document_revision")),
        "effective_date": safe_str(data.get("effective_date")),
        "document_number": safe_str(data.get("document_number")),
    }
    import datetime as _dt
    from .config import optional_env as _opt_env
    base_record["document_revision"] = cover_meta["document_revision"]
    base_record["effective_date"] = cover_meta["effective_date"]
    base_record["document_number"] = cover_meta["document_number"]
    base_record["embedding_version"] = _opt_env(
        "EMBEDDING_MODEL_VERSION", "text-embedding-ada-002"
    )
    base_record["last_indexed_at"] = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    base_record["language"] = "en"

    def _finalize(record: dict[str, Any]) -> dict[str, Any]:
        """Stamp highlight_text + figures_referenced_normalized onto every
        return path so the record carries:
          - sanitized highlight string the citation UI consumes
          - cross-record join key matching text-record
            `figures_referenced_normalized`. Diagrams emit a
            single-element list (or empty) so the field type
            (Collection(Edm.String)) is the same across record types
            and frontend filters can use one query shape:
              figures_referenced_normalized/any(f: f eq '18117')
        """
        record["highlight_text"] = build_highlight_text(record.get("diagram_description", ""))
        ref = record.get("figure_ref") or ""
        norm = normalize_figure_ref(ref)
        record["figures_referenced_normalized"] = [norm] if norm else []
        return record

    if not image_b64:
        # Try fetching the crop from blob cache (pre-computed by preanalyze)
        if source_path and figure_id:
            crop_data = fetch_cached_crop(source_path, figure_id)
            if crop_data:
                image_b64 = crop_data.get("image_b64", "")
                if not bbox:
                    bbox = crop_data.get("bbox", {})
                    # Same list-wrapping rationale as the primary path —
                    # keeps the citation contract uniform across record types.
                    bbox_json = json.dumps([bbox], separators=(",", ":")) if bbox else ""
                logging.info("fetched crop from cache for %s/%s", source_file, figure_id)
                # Recompute hash + chunk_id + bbox_json because they were
                # originally computed on empty image_b64. phash stays
                # deferred (lazy) -- only computed if/when we reach the
                # phash cache lookup, not for crop fetch alone.
                raw_bytes = _decode_b64_once(image_b64)
                img_hash = _image_hash(image_b64, raw=raw_bytes)
                chunk_id = diagram_chunk_id(source_path, source_file, img_hash)
                base_record["chunk_id"] = chunk_id
                base_record["image_hash"] = img_hash
                base_record["figure_bbox"] = bbox_json

    if not image_b64:
        return _finalize({
            **base_record,
            "has_diagram": False,
            "diagram_description": "",
            "diagram_category": "unknown",
            "figure_ref": "",
            "processing_status": "no_image",
        })

    # Trace breadcrumb so worker crashes leave a hint in App Insights about
    # which figure was being processed. Each step in process_diagram logs
    # a single info line so the LAST log line before a crash identifies
    # the precise operation that killed the worker.
    img_size = len(raw_bytes) if raw_bytes is not None else 0
    logging.info(
        "diagram start: src=%s fig=%s img_bytes=%d",
        (source_file or "")[-40:], figure_id, img_size,
    )

    # ── Image triage: skip tiny crops ──
    # Reuse raw_bytes from the _decode_b64_once call above instead of
    # decoding again. On a 5MB crop × 500 figures/PDF the redundant decode
    # was ~100s of CPU on the critical path. Fall back to a fresh decode
    # only if raw_bytes is None (decode failed earlier).
    raw_png = raw_bytes if raw_bytes is not None else _decode_b64_once(image_b64)
    if raw_png is not None and len(raw_png) < MIN_CROP_BYTES:
        logging.info("triage skip (tiny %d bytes) for %s/%s", len(raw_png), source_file, figure_id)
        return _finalize({
            **base_record,
            "has_diagram": False,
            "diagram_description": "",
            "diagram_category": "decorative",
            "figure_ref": "",
            "processing_status": "skipped_tiny",
        })

    # ── Fast path: pre-computed vision result from preanalyze.py ──
    if source_path and figure_id:
        precomputed = fetch_precomputed_vision(source_path, figure_id)
        if precomputed:
            p_category = (precomputed.get("category") or "unknown").strip().lower()
            if p_category not in VALID_CATEGORIES:
                p_category = "unknown"
            p_description = safe_str(precomputed.get("description")).strip()
            p_figure_ref = safe_str(precomputed.get("figure_ref")).strip()
            p_ocr_text = safe_str(precomputed.get("ocr_text")).strip()
            p_has_diagram = (
                bool(precomputed.get("is_useful"))
                and p_category in USEFUL_CATEGORIES
                and bool(p_description)
            )
            if not p_figure_ref:
                m = FIGURE_REF_RE.search(caption or surrounding or "")
                if m:
                    p_figure_ref = f"{m.group(1).title()} {m.group(2)}"
            full_desc = p_description
            if p_ocr_text:
                full_desc = f"{p_description}\nLabels: {p_ocr_text}"

            # img_hash + chunk_id already computed at top of function;
            # base_record already carries them. phash stays "" on this
            # path -- not needed for cross-PDF dedup of precomputed
            # records (the FIRST occurrence's record carries phash from
            # the slow path; subsequent records are findable via the
            # primary sha256 cache).
            logging.info("using precomputed vision for %s/%s", source_file, figure_id)
            return _finalize({
                **base_record,
                "has_diagram": p_has_diagram,
                "diagram_description": full_desc,
                "diagram_category": p_category,
                "figure_ref": p_figure_ref,
                "processing_status": "precomputed",
            })

    cached = lookup_existing_by_hash(parent_id, img_hash)
    if cached:
        return _finalize({
            **base_record,
            "has_diagram": bool(cached.get("has_diagram")),
            "diagram_description": safe_str(cached.get("diagram_description")),
            "diagram_category": safe_str(cached.get("diagram_category"), "unknown"),
            "figure_ref": safe_str(cached.get("figure_ref")),
            "processing_status": "cache_hit",
        })

    # Cross-PDF perceptual-hash lookup. Gated by env flag
    # SEARCH_CACHE_CROSS_PARENT — off by default so this is opt-in.
    # When enabled, the same OEM nameplate appearing in 50 manuals
    # collapses to one vision call (the first manual indexed pays;
    # the rest hit cache via phash). The trade-off is that the
    # cached caption / surrounding_context come from a different
    # manual; for nameplates and other context-independent figures
    # that's fine, but for figures whose meaning depends on the
    # surrounding manual it can be misleading. Evaluate per corpus.
    #
    # phash is computed lazily HERE (was eager at top of function).
    # Skipping it on the precomputed-vision / sha256-cache-hit paths
    # saves ~80-200ms PIL work per figure × 200 figures = 30-50s/PDF.
    if not img_phash:
        img_phash = _image_phash(image_b64, raw=raw_bytes)
        base_record["image_phash"] = img_phash
    if img_phash:
        cached_phash = lookup_existing_by_phash(img_phash)
        if cached_phash:
            return _finalize({
                **base_record,
                "has_diagram": bool(cached_phash.get("has_diagram")),
                "diagram_description": safe_str(cached_phash.get("diagram_description")),
                "diagram_category": safe_str(cached_phash.get("diagram_category"), "unknown"),
                "figure_ref": safe_str(cached_phash.get("figure_ref")),
                "processing_status": "cache_hit_phash",
            })

    # Narrowed exception scope: bare `except Exception` here swallowed
    # AAD/credential failures (azure.core ClientAuthenticationError,
    # openai.AuthenticationError) silently, emitting empty diagram records
    # with processing_status="vision_error:..." that then poison the
    # search_cache (cache filter requires status='ok' to retry). A partial
    # AAD outage would index ~all figures as empty. Now: catch only
    # transient API/network/decode errors; let auth, config, and
    # unexpected SDK errors propagate so the skill envelope reports a
    # per-record error that counts toward maxFailedItems.
    try:
        result = _call_vision(image_b64, _build_user_text(data))
    except (TimeoutError, json.JSONDecodeError) as exc:
        return _finalize({
            **base_record,
            "has_diagram": False,
            "diagram_description": "",
            "diagram_category": "unknown",
            "figure_ref": "",
            "processing_status": f"vision_error:{type(exc).__name__}",
        })
    except Exception as exc:
        # OpenAI SDK exceptions: catch the transient family (rate limit,
        # connection, timeout, server) by name without importing the
        # symbols (so unit tests without `openai` installed still pass).
        exc_name = type(exc).__name__
        transient = {
            "APIError", "APIConnectionError", "APITimeoutError",
            "RateLimitError", "InternalServerError", "ServiceUnavailableError",
        }
        if exc_name in transient or isinstance(exc, __import__("httpx").HTTPError):
            return _finalize({
                **base_record,
                "has_diagram": False,
                "diagram_description": "",
                "diagram_category": "unknown",
                "figure_ref": "",
                "processing_status": f"vision_error:{exc_name}",
            })
        # Auth / config / unknown — propagate so the skill error envelope
        # surfaces the failure to the indexer.
        raise

    category = (result.get("category") or "unknown").strip().lower()
    if category not in VALID_CATEGORIES:
        category = "unknown"

    description = safe_str(result.get("description")).strip()
    figure_ref = safe_str(result.get("figure_ref")).strip()
    if not figure_ref:
        m = FIGURE_REF_RE.search(caption or surrounding or "")
        if m:
            figure_ref = f"{m.group(1).title()} {m.group(2)}"

    ocr_text = safe_str(result.get("ocr_text")).strip()

    is_useful = bool(result.get("is_useful"))
    has_diagram = (
        is_useful
        and category in USEFUL_CATEGORIES
        and bool(description)
    )

    full_description = description
    if ocr_text:
        full_description = f"{description}\nLabels: {ocr_text}"

    # Three distinct outcomes -- DON'T conflate "decorative" with
    # "empty description":
    #   - has_diagram=True  -> "ok"        (cached, never re-run)
    #   - is_useful=True but description="" (content-filter near-miss):
    #     -> "vision_empty" (NOT cached; next indexer run retries)
    #   - is_useful=False (decorative/logo/photo) or category not in
    #     USEFUL_CATEGORIES -> "skipped_decorative" (intentional skip,
    #     no retry needed)
    # The previous code conflated #2 with #3, silently caching empty
    # descriptions as skipped_decorative -- which the search_cache
    # filter (status='ok') refused to retry, so every indexer run
    # produced the SAME empty record. This is the "same vision errors
    # repeating across every figure of every document" symptom.
    if has_diagram:
        status = "ok"
    elif is_useful and category in USEFUL_CATEGORIES and not description:
        status = "vision_empty"
    else:
        status = "skipped_decorative"

    return _finalize({
        **base_record,
        "has_diagram": has_diagram,
        "diagram_description": full_description,
        "diagram_category": category,
        "figure_ref": figure_ref,
        "processing_status": status,
    })
