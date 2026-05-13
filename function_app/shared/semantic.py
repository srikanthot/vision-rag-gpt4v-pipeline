"""
build-semantic-string

Builds a single, real string for chunk_for_semantic. Two modes:
  - mode='text'    : assemble from headers + figure_ref + table_ref +
                     page label + (cleaned) chunk
  - mode='diagram' : assemble from figure_ref + category + description +
                     surrounding context

The text-mode string head-loads extracted entity references (figure_ref,
table_ref) on a dedicated `References:` line near the top, so the
embedding model and the semantic ranker both see them as anchors, not
just as inline tokens buried in body prose.

Tech-manual specifics — also head-loaded as anchors on the embedded
form (not on the raw chunk):
  - safety callouts: WARNING / DANGER / CAUTION / NOTE
  - equation references: "Equation 4-2", "Eq. 18.3"
  - section references: "Section 4.2"

Body chunks are run through a running-artifacts strip before embedding
to remove repeating page-headers / page-footers like "Chapter 18 —
continued" or "Page 215 of 600". The raw `chunk` field is preserved
unchanged for the citation UI; only what we embed is cleaned.
"""

import re
from typing import Any

from .ids import safe_str
from .sections import _strip_running_artifacts

# Equation references: "Equation 4-2", "Eq. 18.3", "EQ. A-1"
EQUATION_REF_RE = re.compile(
    r"\b(Equation|Eq\.?)\s*[\-:]?\s*([A-Z]{0,3}[\-\.]?\d[\w\-\.]{0,8})",
    re.IGNORECASE,
)

# Section number references: "Section 4.2", "Sec. 18-3", "§ 4.2.1".
# No leading \b: `§` is non-word so \b doesn't match before it. The
# pattern allows both dots and hyphens between numeric components, which
# is the convention in technical manuals (4.2.1 and 4-2 both appear).
SECTION_REF_RE = re.compile(
    r"(?:Section|Sec\.?|§)\s*([0-9]+(?:[\.\-][0-9]+){0,3})\b",
    re.IGNORECASE,
)

# Safety callouts. DI usually emits these as "WARNING", "DANGER:",
# "CAUTION —", at the start of a line. We capture the keyword + a short
# tail so a user query like "danger gas leak" can hit chunks that
# explicitly contain a danger callout.
SAFETY_CALLOUT_RE = re.compile(
    r"\b(WARNING|DANGER|CAUTION|NOTICE|NOTE)\b[\s:\-–—]+([^\n]{1,200})",
    re.IGNORECASE,
)


def _clean_ref(s: str) -> str:
    return s.rstrip(".-,;:")


def _extract_equation_refs(text: str) -> list[str]:
    if not text:
        return []
    refs = sorted({
        f"Equation {_clean_ref(m.group(2))}"
        for m in EQUATION_REF_RE.finditer(text)
        if _clean_ref(m.group(2))
    })
    return refs


def _extract_section_refs(text: str) -> list[str]:
    if not text:
        return []
    return sorted({
        f"Section {_clean_ref(m.group(1))}"
        for m in SECTION_REF_RE.finditer(text)
        if _clean_ref(m.group(1))
    })


def _extract_callouts(text: str) -> list[str]:
    """Returns ALL safety callouts found, each as 'KEYWORD: short text'.
    Each callout's tail is capped at 200 chars (long callouts are still
    in the raw chunk verbatim — this is just for the head-loaded
    semantic anchor).

    Previously this was capped at the first 3 to limit embedding-token
    spend, but for safety-critical manuals that's a correctness risk:
    a page with 5 distinct WARNING boxes silently lost 2. Embedding
    cost of an extra 200-char prefix per callout is negligible
    compared to the cost of a chatbot missing a DANGER notice.
    """
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in SAFETY_CALLOUT_RE.finditer(text):
        keyword = m.group(1).upper()
        tail = m.group(2).strip()
        if not tail:
            continue
        formatted = f"{keyword}: {tail[:200]}"
        # Dedup exact-match callouts: the same WARNING text repeated on
        # every page of a section would otherwise appear N times.
        if formatted in seen:
            continue
        seen.add(formatted)
        out.append(formatted)
    return out


def extract_callout_keywords(text: str) -> list[str]:
    """Returns the deduped, sorted list of distinct callout *keywords*
    (WARNING / DANGER / CAUTION / NOTICE / NOTE) found in the text. Used
    to populate the `callouts` collection field on text records so a
    chatbot can filter `callouts/any(c: c eq 'DANGER')` and a UI can
    render a 'DANGER' badge on the citation chip.

    The boolean `safety_callout` is `len(extract_callout_keywords(t)) > 0`.
    """
    if not text:
        return []
    found: set[str] = set()
    for m in SAFETY_CALLOUT_RE.finditer(text):
        kw = (m.group(1) or "").upper()
        if kw:
            found.add(kw)
    return sorted(found)


def _join_nonempty(parts: list[str], sep: str) -> str:
    return sep.join([p for p in parts if p])


# Markdown pipe-table block: a header row, separator row, and 1+ body
# rows. We strip these from `chunk_for_semantic` because the same cells
# are already indexed as separate `record_type="table"` (and per-row
# `table_row`) records — embedding them again here is pure cost and
# pollutes the text-record vector with table boilerplate ("| --- | --- |").
# The raw `chunk` field keeps the markdown verbatim for the citation UI.
_MARKDOWN_TABLE_BLOCK_RE = re.compile(
    r"(?:^|\n)"                          # block start
    r"\|[^\n]+\|\s*\n"                   # header row
    r"\|\s*[\-:|\s]+\|\s*\n"              # separator row (--- / :--- / etc.)
    r"(?:\|[^\n]+\|\s*\n?)+",            # one or more body rows
    re.MULTILINE,
)

# HTML table fragments that DI sometimes emits inside markdown when the
# table is too complex to render as a clean pipe-table. We see chunks
# with `<td>$260.00</td><td>$1,070.00</td>` etc. -- noise tokens that
# pollute the embedding vector. Strip the whole table section
# (`<table>...</table>` if present) as well as orphaned `<td>...</td>`
# / `<tr>...</tr>` runs.
_HTML_TABLE_BLOCK_RE = re.compile(
    r"<table[\s\S]*?</table\s*>",
    re.IGNORECASE,
)
_HTML_TABLE_FRAGMENT_RE = re.compile(
    r"(?:<(?:tr|td|th|thead|tbody|tfoot)[^>]*>[\s\S]*?</(?:tr|td|th|thead|tbody|tfoot)\s*>\s*){2,}",
    re.IGNORECASE,
)


def _strip_inline_tables(text: str) -> str:
    """Remove markdown pipe-table blocks AND HTML table fragments from
    text. Preserves a marker line so the embedding still knows a table
    appeared at this point in the chunk's flow.

    Both forms covered:
      - Markdown pipe-tables (`| col | col |\n| --- | --- |\n| v | v |`)
        -- DI's standard format
      - HTML <table>/<td>/<tr> fragments -- DI emits these for complex
        tables that don't fit the pipe-table format. Without stripping,
        the embedding sees `<td>$260.00</td>` style noise.
    """
    if not text:
        return text
    placeholder = "\n[Table omitted from embedding -- indexed separately]\n"
    out = text
    if "|" in out:
        out = _MARKDOWN_TABLE_BLOCK_RE.sub(placeholder, out)
    if "<" in out and ("</td>" in out.lower() or "</tr>" in out.lower()
                       or "</table>" in out.lower()):
        # Full <table>...</table> first (most precise), then orphan
        # <tr>/<td> sequences.
        out = _HTML_TABLE_BLOCK_RE.sub(placeholder, out)
        out = _HTML_TABLE_FRAGMENT_RE.sub(placeholder, out)
    return out


def _build_text_string(data: dict[str, Any]) -> str:
    source_file = safe_str(data.get("source_file"))
    h1 = safe_str(data.get("header_1"))
    h2 = safe_str(data.get("header_2"))
    h3 = safe_str(data.get("header_3"))
    chunk = safe_str(data.get("chunk"))
    page_label = safe_str(data.get("printed_page_label"))
    figure_ref = safe_str(data.get("figure_ref"))
    table_ref = safe_str(data.get("table_ref"))

    header_path = _join_nonempty([h1, h2, h3], " > ")

    header_line = f"Section: {header_path}" if header_path else ""
    page_line = f"Page: {page_label}" if page_label else ""
    source_line = f"Source: {source_file}" if source_file else ""

    # Head-load entity references so a query like "Figure 18.117",
    # "Table 18-3", "Equation 4-5" or "Section 4.2" gets a stronger
    # vector hit on chunks that reference them, not just the
    # figure/table records themselves. Equation + section refs are
    # mined from the raw chunk text below; figure/table refs come
    # from extract-page-label upstream.
    eq_refs = _extract_equation_refs(chunk)
    sec_refs = _extract_section_refs(chunk)
    ref_parts = [r for r in [figure_ref, table_ref] if r]
    if eq_refs:
        ref_parts.append(", ".join(eq_refs))
    if sec_refs:
        ref_parts.append(", ".join(sec_refs))
    ref_line = f"References: {' | '.join(ref_parts)}" if ref_parts else ""

    # Safety callouts (WARNING / DANGER / CAUTION / NOTICE / NOTE).
    # Head-loaded so a search for "warning gas leak" surfaces the
    # chunk with the actual warning, not random body prose.
    callouts = _extract_callouts(chunk)
    callout_line = f"Callouts: {' || '.join(callouts)}" if callouts else ""

    # Strip repeating header/footer artifacts AND inline table blocks so
    # the embedding doesn't learn boilerplate or duplicate table content
    # that's already a separate `record_type="table"` row. `chunk`
    # (the raw markdown) is preserved as-is in the index for the
    # citation UI; only the embedded form is cleaned.
    cleaned_chunk = _strip_inline_tables(_strip_running_artifacts(chunk)).strip()

    return _join_nonempty(
        [source_line, header_line, page_line, ref_line, callout_line, cleaned_chunk],
        "\n",
    )


def _build_diagram_string(data: dict[str, Any]) -> str:
    source_file = safe_str(data.get("source_file"))
    description = safe_str(data.get("diagram_description"))
    category = safe_str(data.get("diagram_category"))
    figure_ref = safe_str(data.get("figure_ref"))
    # Surrounding section text the figure was cropped from. Named
    # context_text in the skill payload to make it clear this is body
    # context, not raw OCR.
    context_text = safe_str(data.get("context_text"))
    page = safe_str(data.get("physical_pdf_page"))

    head_bits = []
    if figure_ref:
        head_bits.append(figure_ref)
    if category:
        head_bits.append(f"({category})")
    head = " ".join(head_bits)

    source_line = f"Source: {source_file}" if source_file else ""
    page_line = f"Page: {page}" if page else ""
    head_line = f"Diagram: {head}" if head else "Diagram"
    desc_line = description.strip()
    # 1000-char cap on context (was 600); matches the larger
    # 400+400 surrounding window in process_document.py so the diagram
    # record carries the full procedural context that grounds the figure.
    context_line = f"Context: {context_text.strip()[:1000]}" if context_text.strip() else ""

    return _join_nonempty(
        [source_line, page_line, head_line, desc_line, context_line],
        "\n",
    )


def process_semantic_string(data: dict[str, Any]) -> dict[str, Any]:
    mode = safe_str(data.get("mode"), "text").lower()
    if mode == "diagram":
        return {"chunk_for_semantic": _build_diagram_string(data)}
    return {"chunk_for_semantic": _build_text_string(data)}
