"""
Walk DI's analyzeResult to build a flat section index that maps a page
number to header_1/header_2/header_3 and the section's text content.

DI's "sections" are nested by hierarchy. Each section has elements[]
that reference paragraphs/figures/tables via JSON pointer paths like
"/paragraphs/0". A paragraph's bounding regions tell us which page it
sits on; we use those to compute (start_page, end_page) for each
section, then resolve headers by walking up the section tree.
"""

import re
from typing import Any

PARAGRAPH_REF_RE = re.compile(r"^/paragraphs/(\d+)$")
SECTION_REF_RE = re.compile(r"^/sections/(\d+)$")


def _paragraph_pages(paragraphs: list[dict[str, Any]], idx: int) -> list[int]:
    if idx < 0 or idx >= len(paragraphs):
        return []
    para = paragraphs[idx]
    pages = []
    for br in para.get("boundingRegions", []) or []:
        pn = br.get("pageNumber")
        if isinstance(pn, int):
            pages.append(pn)
    return pages


def _paragraph_role(paragraphs: list[dict[str, Any]], idx: int) -> str:
    if idx < 0 or idx >= len(paragraphs):
        return ""
    return (paragraphs[idx].get("role") or "").lower()


def _paragraph_content(paragraphs: list[dict[str, Any]], idx: int) -> str:
    if idx < 0 or idx >= len(paragraphs):
        return ""
    return paragraphs[idx].get("content") or ""


def build_section_index(analyze_result: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Returns a list of section descriptors. Each item:
      {
        "section_idx": int,
        "header_1": str,
        "header_2": str,
        "header_3": str,
        "page_start": int,
        "page_end": int,
        "content": str,        # concatenated paragraph content for this section
      }

    Header inheritance: walking sections in document order, we keep a
    running stack of (level, title). When a sectionHeading is encountered
    inside a section, we update the stack at the appropriate level.
    """
    sections = analyze_result.get("sections", []) or []
    paragraphs = analyze_result.get("paragraphs", []) or []

    # First pass: walk the section tree in DFS order so we can carry
    # header_1/2/3 as we descend.
    visited = [False] * len(sections)
    flat: list[dict[str, Any]] = []

    def walk(section_idx: int, hdr_stack: list[tuple[int, str]]):
        if section_idx < 0 or section_idx >= len(sections) or visited[section_idx]:
            return
        visited[section_idx] = True
        section = sections[section_idx]

        local_stack = list(hdr_stack)
        para_indices: list[int] = []
        child_section_indices: list[int] = []

        for ref in section.get("elements", []) or []:
            m_p = PARAGRAPH_REF_RE.match(ref)
            if m_p:
                para_indices.append(int(m_p.group(1)))
                continue
            m_s = SECTION_REF_RE.match(ref)
            if m_s:
                child_section_indices.append(int(m_s.group(1)))
                continue

        # Update header stack from heading-role paragraphs.
        # Track the heading paragraph's own page so we can anchor the
        # section's page_start to where the heading actually appears,
        # rather than to min() of every paragraph DI grouped under this
        # section. DI sometimes drags continuation paragraphs from the
        # previous page into a section's elements[]; using min() in that
        # case yields a page_start that's earlier than the real section
        # start (the bug behind page_resolution_method=header_match
        # returning a too-early page).
        heading_page: int | None = None
        for pidx in para_indices:
            role = _paragraph_role(paragraphs, pidx)
            if role in ("title", "sectionheading"):
                title = _paragraph_content(paragraphs, pidx).strip()
                if not title:
                    continue
                level = 1 if role == "title" else _guess_heading_level(title, local_stack)

                # Dedup guard: skip pushing a heading whose normalized text
                # already exists at any level on the stack. MANGOS manuals
                # frequently emit the chapter title twice — once tagged
                # `title` (level 1) and again as a `sectionHeading` whose
                # numeric prefix is missing, which the fallback in
                # _guess_heading_level promotes to level 2. Without this
                # guard, header_1 == header_2 on every chunk under that
                # chapter (the "Chapter 5 - Meters / Chapter 5 - Meters"
                # bug we observed in production).
                norm_title = _norm_header(title)
                already_present = any(
                    _norm_header(existing) == norm_title
                    for _, existing in local_stack
                )
                if already_present:
                    if heading_page is None:
                        pages = _paragraph_pages(paragraphs, pidx)
                        if pages:
                            heading_page = min(pages)
                    continue

                local_stack = [(lvl, txt) for (lvl, txt) in local_stack if lvl < level]
                local_stack.append((level, title))
                if heading_page is None:
                    pages = _paragraph_pages(paragraphs, pidx)
                    if pages:
                        heading_page = min(pages)

        h1 = _stack_at(local_stack, 1)
        h2 = _stack_at(local_stack, 2)
        h3 = _stack_at(local_stack, 3)

        page_set = set()
        body_chunks: list[str] = []
        for pidx in para_indices:
            for pn in _paragraph_pages(paragraphs, pidx):
                page_set.add(pn)
            body_chunks.append(_paragraph_content(paragraphs, pidx))

        if page_set:
            # Prefer the heading paragraph's page as page_start. Fall back
            # to min(page_set) only when this section has no heading (e.g.
            # the document root section, or an unnamed wrapper section).
            page_start = heading_page if heading_page is not None else min(page_set)
            flat.append({
                "section_idx": section_idx,
                "header_1": h1,
                "header_2": h2,
                "header_3": h3,
                "page_start": page_start,
                "page_end": max(page_set),
                "content": "\n".join([c for c in body_chunks if c]),
            })

        for child_idx in child_section_indices:
            walk(child_idx, local_stack)

    if sections:
        walk(0, [])
        for i in range(len(sections)):
            if not visited[i]:
                walk(i, [])

    # Orphan-paragraph capture. Any paragraphs not referenced by any
    # section's elements[] are unreachable through the walk above —
    # their content still flows through DI's markdown stream (so the
    # SplitSkill chunks them), but they have no header chain attached
    # and no entry in the section index, which means our header_match
    # / fuzzy_match page-resolution paths can't find them either.
    #
    # We collect orphans into per-page synthetic sections so:
    #   1. They appear in the section index for fuzzy_match lookups
    #   2. find_section_for_page returns *something* for orphan pages
    #      instead of None (which would force fall-through to the bbox
    #      fallback every time)
    #
    # Synthetic sections are tagged with header_1="(orphan paragraphs)"
    # so consumers can distinguish them from real headed sections —
    # they should rank *behind* real sections in retrieval.
    referenced_para_indices: set[int] = set()
    for sec in sections:
        for ref in sec.get("elements", []) or []:
            m_p = PARAGRAPH_REF_RE.match(ref)
            if m_p:
                referenced_para_indices.add(int(m_p.group(1)))

    orphans_by_page: dict[int, list[str]] = {}
    for pidx, para in enumerate(paragraphs):
        if pidx in referenced_para_indices:
            continue
        # Skip page-furniture paragraphs that DI tags explicitly —
        # those were never meant to be retrieval content.
        role = (para.get("role") or "").lower()
        if role in ("pageheader", "pagefooter", "pagenumber", "pagebreak"):
            continue
        content = (para.get("content") or "").strip()
        if not content or len(content) < 30:
            # Drop very short orphans (single tokens, page artifacts).
            continue
        for pn in _paragraph_pages(paragraphs, pidx):
            orphans_by_page.setdefault(pn, []).append(content)

    # Compute the set of pages already covered by at least one real
    # section. We only emit orphan sections for pages that NO real
    # section covers — otherwise the orphan would compete with the
    # real section in find_section_for_page (orphan wins by smallest
    # span, even when a wider real section is the right answer for a
    # figure or chunk on that page). The "(orphan paragraphs)" header
    # would then leak into the figure/chunk records.
    real_section_covered_pages: set[int] = set()
    for s in flat:
        for p in range(s["page_start"], s["page_end"] + 1):
            real_section_covered_pages.add(p)

    next_idx = len(sections) + 100000
    for page in sorted(orphans_by_page):
        if page in real_section_covered_pages:
            # A real section already covers this page. Don't emit an
            # orphan that would shadow the real one. Orphan content is
            # still in the markdown stream and gets chunked normally.
            continue
        orphan_content = "\n".join(orphans_by_page[page])
        flat.append({
            "section_idx": next_idx,
            "header_1": "(orphan paragraphs)",
            "header_2": "",
            "header_3": "",
            "page_start": page,
            "page_end": page,
            "content": orphan_content,
            "is_orphan": True,  # explicit flag for find_section_for_page
        })
        next_idx += 1

    return flat


def _norm_header(s: str) -> str:
    """Aggressive normalization for header equality comparisons.
    Lower-cases, collapses whitespace, drops punctuation noise so
    "Chapter 5 - Meters" and "CHAPTER 5  —  Meters." compare equal.
    Used by the heading-stack dedup guard in walk()."""
    if not s:
        return ""
    s = re.sub(r"[\s ]+", " ", s).strip().lower()
    # Treat various dashes and stray punctuation as equivalent so
    # OCR / typography variations don't defeat dedup.
    s = re.sub(r"[–—―\-]+", "-", s)
    s = re.sub(r"[.,:;]+$", "", s)
    return s


def _guess_heading_level(title: str, stack: list[tuple[int, str]]) -> int:
    """
    Cheap heuristic for level when DI only tells us 'sectionHeading'.
    Numbered prefixes like '1', '1.2', '1.2.3' map to levels 1/2/3.
    Otherwise: deepen one level relative to the current stack top, capped at 3.
    """
    m = re.match(r"^(\d+(?:\.\d+){0,2})\b", title)
    if m:
        return min(3, len(m.group(1).split(".")))
    if stack:
        return min(3, stack[-1][0] + 1)
    return 2


def _stack_at(stack: list[tuple[int, str]], level: int) -> str:
    for lvl, txt in stack:
        if lvl == level:
            return txt
    return ""


def find_section_for_page(
    sections_index: list[dict[str, Any]],
    page_number: int,
) -> dict[str, Any] | None:
    """
    Return the most-specific section whose page range covers page_number.
    Most-specific = smallest page span among matches; ties broken by
    document order (later wins, since deeper sections are visited later).

    Orphan sections (synthetic "(orphan paragraphs)" entries) are always
    deprioritized — a real section with ANY page span beats an orphan
    section, even when the orphan has a smaller span. This prevents
    figures and chunks from being attributed to "(orphan paragraphs)"
    when their physical page is also covered by a real chapter section.
    """
    matches = [
        s for s in sections_index
        if s["page_start"] <= page_number <= s["page_end"]
    ]
    if not matches:
        return None
    matches.sort(key=lambda s: (
        1 if s.get("is_orphan") else 0,            # orphans always last
        s["page_end"] - s["page_start"],           # tightest span next
        -s["section_idx"],                         # deepest tie-breaker
    ))
    return matches[0]


def find_section_for_page_range(
    sections_index: list[dict[str, Any]],
    page_start: int,
    page_end: int | None = None,
) -> dict[str, Any] | None:
    """Return the section that *contains* the given page range, not just
    one page within it. Used for tables and other multi-page entities
    where the start page may be a section-boundary edge case (the table
    starts at the bottom of the previous section's last page but the
    bulk of its content is in the next section).

    Strategy:
      1. If page_end is None or equals page_start, defer to
         find_section_for_page (single-page case).
      2. Otherwise prefer the section whose range fully contains
         [page_start, page_end].
      3. Fall back to the section that contains the *majority* of pages
         in the range. The majority section is the one whose content
         the entity is most likely about.
      4. Final fallback: find_section_for_page(page_start) — preserves
         prior behavior so multi-page entities with no clean containment
         don't lose section context entirely.
    """
    if page_end is None or page_end <= page_start:
        return find_section_for_page(sections_index, page_start)

    # Tier 1: full containment.
    fully_containing = [
        s for s in sections_index
        if s["page_start"] <= page_start and s["page_end"] >= page_end
    ]
    if fully_containing:
        # Tightest fit (smallest range) wins; document-order tiebreak
        # picks the deepest section. Orphans always lose to real
        # sections (same rationale as find_section_for_page).
        fully_containing.sort(key=lambda s: (
            1 if s.get("is_orphan") else 0,
            s["page_end"] - s["page_start"],
            -s["section_idx"],
        ))
        return fully_containing[0]

    # Tier 2: majority overlap. Compute, for each section that overlaps
    # the range at all, the count of pages in [page_start, page_end]
    # that fall within the section's range. Pick the section with the
    # most overlap pages. Orphan sections are deprioritized: they only
    # win if no real section overlaps the range at all.
    span_pages = list(range(page_start, page_end + 1))
    best: tuple[int, int, int, dict[str, Any]] | None = None  # (-is_orphan_flag, overlap, -section_idx, section)
    for s in sections_index:
        ps, pe = s["page_start"], s["page_end"]
        overlap = sum(1 for p in span_pages if ps <= p <= pe)
        if overlap == 0:
            continue
        is_real = 0 if s.get("is_orphan") else 1  # real wins over orphan
        # Real sections win regardless of overlap; otherwise higher
        # overlap wins; deeper section breaks ties.
        key = (is_real, overlap, -s["section_idx"])
        if best is None or key > (best[0], best[1], best[2]):
            best = (is_real, overlap, -s["section_idx"], s)
    if best is not None:
        # Re-extract section in 4th position (we expanded the tuple).
        return best[3]

    # Tier 3: defer to legacy single-page lookup on page_start.
    return find_section_for_page(sections_index, page_start)


# Typography normalization for fuzzy substring matching. DI extracts the
# same logical character (em-dash, NBSP, smart quote) inconsistently across
# paragraphs vs. captions vs. body text, so a literal `find()` of a caption
# inside its section content silently fails when one path has "—" and the
# other has " - " or " – ". This map collapses the common mismatches.
_CAPTION_NORMALIZE_TABLE = str.maketrans({
    " ": " ",  # NBSP
    " ": " ", " ": " ", " ": " ",  # en/em/thin space
    "​": "",   # zero-width space
    "‐": "-", "‑": "-", "‒": "-",  # hyphen variants
    "–": "-", "—": "-", "―": "-",  # en/em/horizontal-bar
    "‘": "'", "’": "'",                 # smart single quotes
    "“": '"', "”": '"',                 # smart double quotes
    "­": "",   # soft hyphen
})


def _normalize_for_caption_match(s: str) -> str:
    """Normalize a string for caption / anchor substring matching:
    typography variants collapsed, whitespace runs reduced to a single
    space, NFC-normalized. Preserves character offsets only approximately
    — used for *match* checks, not for slicing the original text."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.translate(_CAPTION_NORMALIZE_TABLE)).strip()


def extract_surrounding_text(
    section_content: str,
    anchor: str,
    chars: int = 200,
) -> str:
    """
    If `anchor` (typically a figure caption) is found in section_content,
    return up to `chars` characters before and after it. Otherwise return
    the first 2*chars characters of the section. Strips simple repeating
    header/footer noise.

    Match strategy (most-precise-first):
      1. Literal `find()` on the cleaned section content. Fast path.
      2. Typography-normalized match (NBSP→space, em-dash→hyphen, smart
         quotes→ASCII, etc.) so a caption with "Figure 4-2 — Bar Type CT"
         still matches a section content that rendered the dash as "-".
      3. Anchor-prefix match (first 40 chars of normalized anchor) to
         catch cases where DI splits the caption across paragraphs.
    Without these fallbacks, a missed caption silently degrades the
    surrounding_context to "first 2*chars of the section" — which is
    usually boilerplate intro text and not the diagram's grounding
    paragraph.
    """
    if not section_content:
        return ""
    cleaned = _strip_running_artifacts(section_content)
    anchor_clean = (anchor or "").strip()
    if anchor_clean:
        idx = cleaned.find(anchor_clean)
        if idx >= 0:
            anchor_end = idx + len(anchor_clean)
            start = max(0, idx - chars)
            end = min(len(cleaned), anchor_end + chars)
            before = cleaned[start:idx].strip()
            after = cleaned[anchor_end:end].strip()
            return f"{before} [...] {after}".strip()
        # Typography-normalized fallback. Compute the normalized form of
        # both sides and locate the anchor; map back to an approximate
        # offset in the original by character-counting equivalence (1:1
        # since our normalization is per-char). Soft-hyphens and ZWSP
        # are dropped (changes lengths by 1) — the slicing here uses
        # the normalized text directly so the windows stay correct.
        norm_section = cleaned.translate(_CAPTION_NORMALIZE_TABLE)
        norm_section = re.sub(r"\s+", " ", norm_section)
        norm_anchor = _normalize_for_caption_match(anchor_clean)
        if norm_anchor:
            n_idx = norm_section.find(norm_anchor)
            if n_idx >= 0:
                n_end = n_idx + len(norm_anchor)
                start = max(0, n_idx - chars)
                end = min(len(norm_section), n_end + chars)
                before = norm_section[start:n_idx].strip()
                after = norm_section[n_end:end].strip()
                return f"{before} [...] {after}".strip()
            # Anchor-prefix fallback: try the first 40 normalized chars.
            # Catches cases where DI reformatted the trailing portion of
            # the caption (line wraps, page breaks).
            probe = norm_anchor[:40]
            if len(probe) >= 20:
                p_idx = norm_section.find(probe)
                if p_idx >= 0:
                    start = max(0, p_idx - chars)
                    end = min(len(norm_section), p_idx + len(probe) + chars)
                    return norm_section[start:end].strip()
    return cleaned[: 2 * chars].strip()


_RUNNING_ARTIFACT_PATTERNS = [
    # "Page 215", "Page 215 of 600". Stripped only at page-block boundaries
    # so a body sentence like "see Page 215 for fault clearing" survives.
    re.compile(r"page\s+\d+(\s+of\s+\d+)?", re.IGNORECASE),
    # NOTE: the bare-numeric pattern (`\d{1,4}`) was deliberately removed.
    # It used to fire `fullmatch`-anywhere and silently dropped:
    #   - footnote markers rendered as "1" / "23" on their own line
    #   - numbered list items that got hard-broken
    #   - standalone numeric values in spec sheets
    # DI emits explicit `<!-- PageNumber="N" -->` markers for printed page
    # numbers, so we don't need a regex fallback for that case. Keeping the
    # bare-numeric strip would discard footnote markers — which is
    # safety-critical content (IEEE / NEC / OSHA citations live in
    # footnotes). DO NOT re-add this pattern without also implementing
    # footnote-marker linkage to body sentences.
    #
    # Same logic for lone Roman numerals — DI markers handle the page-
    # numbering use case; re-introducing a regex strip would drop
    # legitimate content like "Phase III" rendered alone on a wrap line.

    # revision / version footers commonly stamped on every page of a
    # technical manual: "Rev. 3.2", "Revision 4", "Version 1.0",
    # "Issue 2.1 -- March 2024". Position-gated below.
    re.compile(r"(rev|revision|version|issue|ver)\.?\s*\d+(\.\d+)*([\s\-–—].*)?", re.IGNORECASE),
    # standalone copyright lines appearing in headers/footers
    re.compile(r"(copyright|\(c\)|©)\s.*", re.IGNORECASE),
    # "Confidential", "Proprietary", "For Internal Use Only" stamps
    re.compile(r"(confidential|proprietary|internal\s+use\s+only|do\s+not\s+distribute)\.?", re.IGNORECASE),
    # NOTE: the doc-number pattern (`[A-Z]{2,5}-[A-Z0-9]{2,5}(-[A-Z0-9]{1,5}){0,3}`)
    # was deliberately removed. In a 4-line chunk *every* line is at
    # "first 2 OR last 2" of its block, so position gating doesn't protect
    # mid-chunk lines. The pattern collides with legitimate part numbers
    # ("GE-THQL-1120-2"), NEMA model strings, and equipment IDs that
    # appear on their own line in the body. Keeping a few unstripped
    # doc numbers in embeddings is far cheaper than silently dropping
    # part-number cites a chatbot answer would hinge on. If page-footer
    # doc numbers become a real noise source, address them in DI's
    # pageHeader/pageFooter role pass instead of via blanket regex.
    # date footers: "March 2024", "2024-03-15", "03/15/2024"
    re.compile(r"(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(tember)?|oct(ober)?|nov(ember)?|dec(ember)?)\s+\d{4}", re.IGNORECASE),
    re.compile(r"\d{4}[\-/]\d{1,2}[\-/]\d{1,2}"),
    re.compile(r"\d{1,2}[\-/]\d{1,2}[\-/]\d{2,4}"),
]


# Matches the DI markdown markers that delimit page boundaries inside a
# section's content. We split on these to identify per-page blocks and
# then only strip artifact lines at the top/bottom of each block.
_PAGE_MARKER_RE = re.compile(
    r'<!--\s*Page(?:Break|Number\s*=\s*"[^"]*")\s*-->',
    re.IGNORECASE,
)


def _strip_artifacts_in_block(block: str) -> str:
    """Strip running artifacts only at the top/bottom 2 non-empty lines
    of a single page-block. Lines in the middle of a block are preserved
    verbatim — that protects:
      - footnote bodies (which sit mid-page)
      - body sentences that incidentally fullmatch an artifact pattern
        (e.g. "March 2024" alone on a wrap line)
      - part numbers / model numbers ("GE-THQL-1120-2") on their own line
    """
    if not block:
        return ""
    lines = block.splitlines()
    non_empty = [(i, ln.strip()) for i, ln in enumerate(lines) if ln.strip()]
    if not non_empty:
        return ""
    # Eligible-for-strip set: first 2 + last 2 non-empty line positions.
    eligible: set[int] = set()
    eligible.update(idx for idx, _ in non_empty[:2])
    eligible.update(idx for idx, _ in non_empty[-2:])

    out: list[str] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        if i in eligible and any(p.fullmatch(s) for p in _RUNNING_ARTIFACT_PATTERNS):
            continue
        out.append(s)
    return "\n".join(out)


def _strip_running_artifacts(text: str) -> str:
    """Strip lines that look like repeating page headers/footers, but only
    at the top/bottom of each page-block. The page-block boundaries come
    from DI's `<!-- PageBreak -->` and `<!-- PageNumber="..." -->`
    markers when present; otherwise the entire input is treated as one
    block.

    Why position-gate: a `fullmatch`-anywhere strip silently drops any
    line that happens to look like a footer (a lone "1", a date, a
    part number) regardless of where it sits in the document. For
    safety-critical manuals that's a content-loss risk — footnote
    markers, numbered list items, spec-sheet values can all appear
    on their own line in the middle of a page and must be preserved.
    Real headers/footers always sit at page edges; gating to the top
    and bottom 2 non-empty lines per page-block strips them while
    preserving body content.
    """
    if not text:
        return text
    if _PAGE_MARKER_RE.search(text):
        # Has DI markers — process each page-block independently. The
        # markers themselves are dropped (they were noise tokens for
        # the embedding anyway).
        blocks = _PAGE_MARKER_RE.split(text)
        cleaned = [_strip_artifacts_in_block(b) for b in blocks]
        return "\n".join(b for b in cleaned if b.strip())
    # No markers — whole text is one block. Used by extract_surrounding_text
    # which operates on concatenated paragraph content (already marker-free).
    return _strip_artifacts_in_block(text)
