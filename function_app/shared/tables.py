"""
Convert DI's tables[] into structured markdown table records.

Features:
- Builds a markdown grid honoring rowIndex/columnIndex (no spans collapsed,
  spanned cells repeat their content for readability).
- Detects multi-page table continuations: a follow-on table on the next
  page with the same column count and no caption is merged.
- Splits oversized tables (>3000 chars) at row boundaries, repeating the
  header row in each split.
- Emits per-row records (record_type="table_row") for tables with 5–80
  body rows, so cell-level queries ("200A 4-wire 277/480V conductor?")
  retrieve the relevant row directly without the LLM having to traverse
  a markdown grid.
"""

import logging
from typing import Any

MAX_TABLE_CHARS = 3000

# Row-record emission: emit per-row records for tables of meaningful size.
#
# Lower bound (5): below 5 rows the parent table record is enough; a 3-row
# table doesn't add retrieval value as 3 separate row records.
#
# Upper bound (5000): matches _MAX_TABLE_ROWS, the defensive cap on DI's
# rowCount. MANGOS technical manuals (pricing schedules, conductor spec
# tables, equipment rating tables) routinely have 100-500-row tables --
# those rows ARE the high-value retrieval content (queries like
# "200A 4-wire 277/480V conductor 4/0" need to hit the specific row).
#
# Audit 2026-05-13: previous cap of 80 was excluding 70,000+ row records
# across the MANGOS corpus, causing the index to come in at ~21K chunks
# vs v04's 92K. Per-row emission for large tables is the right call for
# technical-manual retrieval -- index size grows but the rows are
# exactly the lookup keys the chatbot needs.
ROW_RECORD_MIN_ROWS = 5
ROW_RECORD_MAX_ROWS = 5000


def _cell_text(cell: dict[str, Any]) -> str:
    return (cell.get("content") or "").replace("|", "\\|").replace("\n", " ").strip()


def _table_pages(table: dict[str, Any]) -> list[int]:
    pages = set()
    for br in table.get("boundingRegions", []) or []:
        pn = br.get("pageNumber")
        if isinstance(pn, int):
            pages.add(pn)
    return sorted(pages)


def _table_caption(table: dict[str, Any]) -> str:
    cap = table.get("caption") or {}
    return (cap.get("content") or "").strip()


_MAX_TABLE_ROWS = 5000
_MAX_TABLE_COLS = 200


def _table_to_grid(table: dict[str, Any]) -> list[list[str]]:
    rows = table.get("rowCount") or 0
    cols = table.get("columnCount") or 0
    # Defensive caps: DI occasionally emits malformed tables with
    # rowCount/columnCount in the thousands (mis-detected list as table).
    # A 10000x100 table = 1M cells × 80 bytes ≈ 80MB of empty strings.
    # 530 such tables × 80MB = 42GB, instant worker OOM. Reject early.
    if rows > _MAX_TABLE_ROWS or cols > _MAX_TABLE_COLS:
        logging.warning(
            "_table_to_grid: rejecting table with %d rows × %d cols "
            "(caps: %d × %d). Likely DI mis-detection.",
            rows, cols, _MAX_TABLE_ROWS, _MAX_TABLE_COLS,
        )
        return []
    if rows <= 0 or cols <= 0:
        return []
    grid = [["" for _ in range(cols)] for _ in range(rows)]
    for cell in table.get("cells", []) or []:
        r = cell.get("rowIndex", 0)
        c = cell.get("columnIndex", 0)
        rs = cell.get("rowSpan", 1) or 1
        cs = cell.get("columnSpan", 1) or 1
        text = _cell_text(cell)
        for dr in range(rs):
            for dc in range(cs):
                rr, cc = r + dr, c + dc
                if 0 <= rr < rows and 0 <= cc < cols:
                    grid[rr][cc] = text
    return grid


def _header_row_count(table: dict[str, Any]) -> int:
    """How many leading rows of the table are header rows.

    MANGOS manuals routinely use 2-row or 3-row table headers (super-header
    + sub-header), e.g. a "Voltage" super-header spanning two columns
    above "120/240" and "277/480" sub-headers. The naive "row 0 is the
    header" assumption flattens that hierarchy and the chunk no longer
    answers cell-level questions like "for 200A 4-wire 277/480V, what
    conductor size?".

    Strategy: walk DI's `cells` array and find leading rows where the
    majority of original (non-replicated) cells are tagged
    `kind: "columnHeader"`. Falls back to 1 when DI didn't populate
    `kind` (older models / OCR'd PDFs) — preserves the prior behavior
    for tables without explicit header tagging.
    """
    cells = table.get("cells", []) or []
    if not cells:
        return 1
    row_count = table.get("rowCount") or 0
    if row_count == 0:
        return 1

    # Group cells by their *original* row position (rowIndex of the
    # cell record itself, not replicated positions covered by spans).
    rows_to_kinds: dict[int, list[str]] = {}
    for cell in cells:
        ri = cell.get("rowIndex", 0)
        kind = (cell.get("kind") or "").lower()
        rows_to_kinds.setdefault(ri, []).append(kind)

    leading_header_rows = 0
    for r in range(row_count):
        kinds = rows_to_kinds.get(r) or []
        if not kinds:
            break
        header_count = sum(1 for k in kinds if k == "columnheader")
        # ≥50% of original cells in the row tagged as columnHeader
        # means this whole row is header. (DI sometimes only tags the
        # leftmost / representative cells, so we don't require all.)
        if header_count > 0 and header_count >= len(kinds) / 2:
            leading_header_rows += 1
        else:
            break

    return leading_header_rows if leading_header_rows >= 1 else 1


def _fold_headers(grid: list[list[str]], header_rows: int) -> list[str]:
    """Combine multi-row headers into one header row by joining each
    column's stacked header cells with " — ".

    Input grid:           Output for header_rows=2:
        | "" | Voltage | Voltage |        | Service Class | Voltage — 120/240 | Voltage — 277/480 |
        | Service Class | 120/240 | 277/480 |
        | 200A | 4-wire | 4-wire |

    The folded header preserves the column hierarchy in the embedding
    text so semantic search and the LLM both see the relationship
    between "Voltage" and "277/480" — without that, "200A 4-wire
    277/480" is unanswerable from the table alone.

    Duplicate values stacked in one column (a super-header that spans
    multiple columns is replicated across them by _table_to_grid) are
    de-duplicated so we don't emit "Voltage — Voltage — 120/240"."""
    if header_rows <= 0 or not grid:
        return []
    if header_rows == 1:
        return list(grid[0])
    cols = len(grid[0]) if grid else 0
    folded: list[str] = []
    for c in range(cols):
        # Walk top-to-bottom for this column; collect non-empty,
        # non-duplicate cell values.
        seen: list[str] = []
        for r in range(min(header_rows, len(grid))):
            v = (grid[r][c] if c < len(grid[r]) else "").strip()
            if not v:
                continue
            if seen and seen[-1] == v:
                continue  # super-header replicated across cells
            seen.append(v)
        folded.append(" — ".join(seen))
    return folded


def _grid_to_markdown(grid: list[list[str]], header_rows: int = 1) -> str:
    if not grid or not grid[0]:
        return ""
    folded_header = _fold_headers(grid, header_rows)
    if not folded_header:
        folded_header = list(grid[0])
        header_rows = 1
    sep = ["---"] * len(folded_header)
    lines = [
        "| " + " | ".join(folded_header) + " |",
        "| " + " | ".join(sep) + " |",
    ]
    for row in grid[header_rows:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _try_merge_continuation(prev: dict[str, Any], curr: dict[str, Any]) -> bool:
    """
    True if `curr` looks like a continuation of `prev`:
      - immediately following page
      - same column count
      - curr has no caption
    """
    prev_pages = _table_pages(prev)
    curr_pages = _table_pages(curr)
    if not prev_pages or not curr_pages:
        return False
    if curr_pages[0] != prev_pages[-1] + 1:
        return False
    if (prev.get("columnCount") or 0) != (curr.get("columnCount") or 0):
        return False
    if _table_caption(curr):
        return False
    return True


def _header_block_matches(
    prev_grid: list[list[str]],
    curr_grid: list[list[str]],
    header_rows: int,
) -> bool:
    """True if curr_grid's first `header_rows` rows match prev_grid's
    first `header_rows` rows. Used to detect headers DI repeats on a
    continuation page so they can be dropped from the merged body.

    Multi-row aware: when a table has 2 or 3 header rows, DI typically
    repeats ALL of them on the continuation. The previous single-row
    check kept rows 1..N-1 of the duplicated header as data rows, which
    polluted the body. This block-aware check drops the full header
    stack."""
    if not prev_grid or not curr_grid:
        return False
    if header_rows <= 0:
        return False
    if len(prev_grid) < header_rows or len(curr_grid) < header_rows:
        return False
    return prev_grid[:header_rows] == curr_grid[:header_rows]


def _split_oversized(markdown: str) -> list[str]:
    if len(markdown) <= MAX_TABLE_CHARS:
        return [markdown]
    lines = markdown.splitlines()
    if len(lines) < 3:
        return [markdown]
    header = lines[0]
    sep = lines[1]
    body = lines[2:]

    out: list[str] = []
    cur: list[str] = []
    cur_len = len(header) + len(sep) + 2
    for row in body:
        if cur_len + len(row) + 1 > MAX_TABLE_CHARS and cur:
            out.append("\n".join([header, sep, *cur]))
            cur = []
            cur_len = len(header) + len(sep) + 2
        cur.append(row)
        cur_len += len(row) + 1
    if cur:
        out.append("\n".join([header, sep, *cur]))
    return out


def _build_row_records_for_cluster(
    cluster: list[dict[str, Any]],
    grid: list[list[str]],
    header_rows: int,
) -> list[dict[str, Any]]:
    """Render per-row records for the cluster's body rows.

    Each row becomes "{header_1_folded}: {value_1}; {header_2_folded}: {value_2}; ..."
    so a query like "200A 4-wire 277/480V conductor 4/0" hits the row
    directly via BM25 + vector search. The parent's caption and
    section path travel separately on the row record so the chatbot
    can render a "Table 18-3, page 5-7, row 4: ..." citation.

    Returns a list of dicts with row_index, row_text, page (the source
    DI table's first page — close enough for citation), and a
    cluster-relative row_index for ordering. Empty list when the table
    falls outside ROW_RECORD_MIN_ROWS..MAX bounds (we only emit row
    records where they buy retrieval).
    """
    body_rows = grid[header_rows:]
    if not (ROW_RECORD_MIN_ROWS <= len(body_rows) <= ROW_RECORD_MAX_ROWS):
        return []
    folded_headers = _fold_headers(grid, header_rows)
    if not folded_headers:
        return []

    # Map merged-grid body rows to their source-table page. Walk the
    # cluster in order, mirroring the merge logic that trims duplicated
    # header blocks on continuation pages so row counts add up correctly.
    row_to_page: list[int | None] = []
    prev_grid = None
    for tbl_idx, tbl in enumerate(cluster):
        tbl_grid = _table_to_grid(tbl)
        pages = _table_pages(tbl)
        page_for_rows = pages[0] if pages else None
        if tbl_idx == 0:
            contributed = max(0, len(tbl_grid) - header_rows)
        else:
            # Continuation: merge code dropped the duplicated header block
            # iff _header_block_matches returned True. Replicate that
            # check to know how many rows this continuation contributed.
            assert prev_grid is not None
            if _header_block_matches(prev_grid, tbl_grid, header_rows):
                contributed = max(0, len(tbl_grid) - header_rows)
            else:
                contributed = len(tbl_grid)
        for _ in range(contributed):
            row_to_page.append(page_for_rows)
        prev_grid = tbl_grid

    out: list[dict[str, Any]] = []
    for i, row_cells in enumerate(body_rows):
        # Render as "Header: value" pairs joined with semicolons. Empty
        # values are skipped so a sparse row doesn't render as
        # "H1:; H2:; H3: x;" with stray separators.
        parts: list[str] = []
        for h, v in zip(folded_headers, row_cells):
            v_clean = (v or "").strip()
            if not v_clean:
                continue
            h_clean = (h or "").strip()
            if h_clean:
                parts.append(f"{h_clean}: {v_clean}")
            else:
                # Header column was empty (e.g. row-label leftmost
                # column). Emit value alone so it still indexes.
                parts.append(v_clean)
        if not parts:
            continue
        row_text = "; ".join(parts)
        page = row_to_page[i] if i < len(row_to_page) else None
        out.append({
            "row_index": i,
            "row_text": row_text,
            "page": page,
        })
    return out


def _bboxes_for_cluster(cluster: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build per-page union bboxes for a merged-table cluster.

    A cluster is a list of one-or-more DI tables that we treated as
    continuations of the same logical table. Each underlying table
    has its own boundingRegions[*] (one polygon per page it spans);
    we union them per page so the citation UI can highlight the
    *whole* table on each page, including continuation pages.

    Returns: list of {page, x_in, y_in, w_in, h_in}, sorted by page.
    Empty list if no usable bounding regions exist.
    """
    by_page: dict[int, tuple[float, float, float, float]] = {}
    for tbl in cluster:
        for br in tbl.get("boundingRegions", []) or []:
            page = br.get("pageNumber")
            polygon = br.get("polygon") or []
            if not isinstance(page, int) or len(polygon) < 8:
                continue
            try:
                xs = polygon[0::2]
                ys = polygon[1::2]
                x0, x1 = float(min(xs)), float(max(xs))
                y0, y1 = float(min(ys)), float(max(ys))
            except (TypeError, ValueError):
                continue
            existing = by_page.get(page)
            if existing is None:
                by_page[page] = (x0, y0, x1, y1)
            else:
                ex0, ey0, ex1, ey1 = existing
                by_page[page] = (
                    min(ex0, x0), min(ey0, y0),
                    max(ex1, x1), max(ey1, y1),
                )
    out: list[dict[str, Any]] = []
    for page in sorted(by_page):
        x0, y0, x1, y1 = by_page[page]
        out.append({
            "page": page,
            "x_in": round(x0, 3),
            "y_in": round(y0, 3),
            "w_in": round(x1 - x0, 3),
            "h_in": round(y1 - y0, 3),
        })
    return out


def extract_table_records(analyze_result: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Returns a list of table record dicts:
      {
        index: int,                # table index (post-merge, post-split)
        page_start, page_end: int,
        markdown: str,
        row_count, col_count: int,
        caption: str,
        bboxes: list[{page, x_in, y_in, w_in, h_in}],  # per-page union;
                                                       # all splits of one
                                                       # logical table share
                                                       # the same list.
      }
    """
    raw_tables = analyze_result.get("tables", []) or []
    if not raw_tables:
        return []

    sorted_tables = sorted(
        raw_tables,
        key=lambda t: (_table_pages(t)[0] if _table_pages(t) else 0),
    )

    # Each merged entry: (cluster_tables, grid, header_rows). header_rows
    # comes from the first table in the cluster (continuation pages
    # repeat the same header structure).
    merged: list[tuple[list[dict[str, Any]], list[list[str]], int]] = []
    for tbl in sorted_tables:
        if merged and _try_merge_continuation(merged[-1][0][-1], tbl):
            prev_grid = merged[-1][1]
            header_rows = merged[-1][2]
            new_grid = _table_to_grid(tbl)
            # DI often repeats the FULL header block (all N header rows)
            # on continuation pages. The block-aware match drops every
            # repeated header row so the merged body has clean data rows.
            if _header_block_matches(prev_grid, new_grid, header_rows):
                new_grid = new_grid[header_rows:]
            prev_grid.extend(new_grid)
            merged[-1][0].append(tbl)
        else:
            merged.append(([tbl], _table_to_grid(tbl), _header_row_count(tbl)))

    out: list[dict[str, Any]] = []
    for cluster_idx, (cluster, grid, header_rows) in enumerate(merged):
        first = cluster[0]
        last = cluster[-1]
        pages_first = _table_pages(first)
        pages_last = _table_pages(last)
        # PDFs are 1-indexed. Defaulting to 0 would produce invalid page
        # filters and broken citations, so fall back to 1 when DI didn't
        # emit bounding regions for this table.
        page_start = pages_first[0] if pages_first else 1
        page_end = pages_last[-1] if pages_last else page_start
        caption = _table_caption(first)
        md = _grid_to_markdown(grid, header_rows=header_rows)
        cluster_bboxes = _bboxes_for_cluster(cluster)

        # Per-row records (only for tables with 5..80 body rows). All
        # splits of one logical table share the same row-record list —
        # row records are cluster-level, not split-level, since they're
        # already at row granularity.
        row_records = _build_row_records_for_cluster(cluster, grid, header_rows)

        for split_idx, chunk in enumerate(_split_oversized(md)):
            # Count data rows in this split chunk (total lines minus header
            # + separator), so callers see the real shape of the split.
            chunk_rows = max(0, len(chunk.splitlines()) - 2)
            out.append({
                "index": f"{cluster_idx}_{split_idx}",
                "page_start": page_start,
                "page_end": page_end,
                "markdown": chunk,
                "row_count": chunk_rows,
                "col_count": len(grid[0]) if grid else 0,
                "caption": caption,
                "bboxes": cluster_bboxes,
                # Only attach row records to the first split — keeps
                # row records emitted exactly once per logical table
                # rather than duplicated across every oversized split.
                "table_rows": row_records if split_idx == 0 else [],
            })

    return out
