"""
Quick diagnostic: query the index to see what record types exist,
check counts, and sample records. Uses AAD auth (same as deploy_search.py).

Usage:
    python scripts/check_index.py --config deploy.config.json
    python scripts/check_index.py --config deploy.config.json --coverage
    python scripts/check_index.py --config deploy.config.json --coverage --write-status
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential

API_VERSION = "2024-05-01-preview"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def coverage_report(cfg: dict, endpoint: str, index_name: str, headers: dict,
                    *, write_status: bool = False,
                    triggered_by: str = "manual") -> dict[str, Any]:
    """Compare PDFs in blob against records in index. Show done / partial / not-started.
    Returns a structured stats dict that callers (and the Cosmos writer) can use."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from preanalyze import list_pdfs

    base = f"{endpoint}/indexes/{index_name}/docs/search?api-version={API_VERSION}"

    print("Listing PDFs in blob container...")
    blob_pdfs = set(list_pdfs(cfg))
    print(f"  {len(blob_pdfs)} PDFs in blob\n")

    print("Querying index for source_file facet (PDFs with any record)...")
    # Use "facets" (plural array) — Azure Search ignores singular "facet"
    # in the JSON body and silently returns 0 results. count:200 returns up
    # to 200 distinct source_file values (we have 56 PDFs, so 200 is plenty).
    body = {"search": "*", "facets": ["source_file,count:200"], "top": 0, "count": True}
    resp = httpx.post(base, json=body, headers=headers, timeout=60)
    if resp.status_code != 200:
        raise SystemExit(
            f"facet query failed: HTTP {resp.status_code}\n{resp.text[:500]}"
        )
    data = resp.json()
    total_chunks = data.get("@odata.count", 0)
    # Facets live under "@search.facets", NOT "@odata.facets" (Azure Search
    # uses two different prefixes in the same response).
    facets = data.get("@search.facets", {}).get("source_file", [])
    started = {f["value"]: f["count"] for f in facets}
    print(f"  {total_chunks} total chunks across {len(started)} PDFs\n")

    print("Querying summary records (PDFs fully end-to-end processed)...")
    body = {
        "search": "*",
        "filter": "record_type eq 'summary'",
        "select": "source_file",
        "top": 1000,
        "count": True,
    }
    resp = httpx.post(base, json=body, headers=headers, timeout=60)
    data = resp.json()
    fully_done = {h["source_file"] for h in data.get("value", []) if h.get("source_file")}
    print(f"  {len(fully_done)} PDFs have a summary record\n")

    not_started = sorted(blob_pdfs - started.keys())
    in_progress = sorted(started.keys() - fully_done - (started.keys() - blob_pdfs))
    done = sorted(fully_done & blob_pdfs)
    orphan = sorted(started.keys() - blob_pdfs)

    bar = "=" * 70
    print(bar)
    print(f"  Total PDFs in blob:           {len(blob_pdfs)}")
    print(f"  Fully chunked (done):         {len(done)}")
    print(f"  Partial / in-progress:        {len(in_progress)}")
    print(f"  Not started:                  {len(not_started)}")
    if orphan:
        print(f"  In index but blob deleted:    {len(orphan)}")
    print(bar)

    if done:
        print("\n-- DONE (summary record present) --")
        for n in done:
            print(f"  ok       {n}  ({started.get(n, 0)} chunks)")
    if in_progress:
        print("\n-- PARTIAL / IN-PROGRESS (chunks exist but no summary record) --")
        for n in in_progress:
            print(f"  partial  {n}  ({started.get(n, 0)} chunks so far)")
    if not_started:
        print("\n-- NOT STARTED (no records in index) --")
        for n in not_started:
            print(f"  todo     {n}")
    if orphan:
        print("\n-- ORPHANED (records in index but PDF no longer in blob) --")
        for n in orphan:
            print(f"  orphan   {n}  ({started.get(n, 0)} chunks)")

    coverage = {
        "blob_pdfs_total": len(blob_pdfs),
        "fully_chunked": len(done),
        "partial": len(in_progress),
        "not_started": len(not_started),
        "orphaned": len(orphan),
        "total_chunks": total_chunks,
        "done_pdfs": done,
        "in_progress_pdfs": in_progress,
        "not_started_pdfs": not_started,
        "orphan_pdfs": orphan,
    }

    if write_status:
        _persist_status_to_cosmos(cfg, coverage, started, fully_done, triggered_by)

    return coverage


def _persist_status_to_cosmos(cfg: dict, coverage: dict[str, Any],
                                started: dict[str, int],
                                fully_done: set[str],
                                triggered_by: str) -> None:
    """Write per-PDF state + a coverage run record. Best-effort."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import cosmos_writer

    if not cosmos_writer._is_configured(cfg):
        print("\n  Cosmos DB not configured -- skipping status persistence.")
        print("  Add a `cosmos` section to deploy.config.json to enable.")
        return

    now = _now_iso()
    pdf_states = []

    for pdf in coverage["done_pdfs"]:
        pdf_states.append({
            "source_file": pdf,
            "status": "done",
            "chunks_in_index": started.get(pdf, 0),
            "last_indexed_at": now,
            "last_error": None,
        })
    for pdf in coverage["in_progress_pdfs"]:
        pdf_states.append({
            "source_file": pdf,
            "status": "partial",
            "chunks_in_index": started.get(pdf, 0),
            "last_indexed_at": now,
            "last_error": None,
        })
    for pdf in coverage["not_started_pdfs"]:
        pdf_states.append({
            "source_file": pdf,
            "status": "not_started",
            "chunks_in_index": 0,
            "last_indexed_at": None,
            "last_error": None,
        })

    n = cosmos_writer.write_pdf_states_bulk(cfg, pdf_states)
    print(f"\n  Cosmos: wrote {n}/{len(pdf_states)} pdf_state rows")

    # Orphans: don't write a state row (we don't know what to claim about
    # them), but log into the run record so the dashboard can flag them.
    cosmos_writer.write_run_record(cfg, {
        "run_type": "coverage",
        "triggered_by": triggered_by,
        "blob_pdfs_total": coverage["blob_pdfs_total"],
        "fully_chunked": coverage["fully_chunked"],
        "partial": coverage["partial"],
        "not_started": coverage["not_started"],
        "orphaned": coverage["orphaned"],
        "total_chunks": coverage["total_chunks"],
        "orphan_pdfs": coverage["orphan_pdfs"],
    })
    print("  Cosmos: wrote run_history record")


def check_stuck_indexer(endpoint: str, prefix: str, headers: dict) -> int:
    """Probe the indexer status. Exit codes:
       0 = healthy
       2 = stuck (in_progress > 24h, OR last 5 runs all failed)
       3 = unable to fetch status (network / auth)

    Designed to be wired as a periodic alert (App Insights / Action Group)
    so an indexer stuck on a bad PDF gets noticed within hours, not days.
    """
    indexer_name = f"{prefix}-indexer"
    url = f"{endpoint}/indexers/{indexer_name}/status?api-version={API_VERSION}"
    try:
        resp = httpx.get(url, headers=headers, timeout=30)
    except Exception as exc:
        print(f"FAIL: cannot fetch indexer status: {exc}", file=sys.stderr)
        return 3
    if resp.status_code != 200:
        print(f"FAIL: status returned {resp.status_code}", file=sys.stderr)
        return 3

    body = resp.json()
    overall = body.get("status", "unknown")
    last = body.get("lastResult") or {}
    history = body.get("executionHistory") or []

    print(f"  overall.status        : {overall}")
    print(f"  lastResult.status     : {last.get('status')}")
    print(f"  lastResult.start      : {last.get('startTime')}")
    print(f"  lastResult.items      : {last.get('itemsProcessed', 0)}")
    print(f"  lastResult.errors     : {len(last.get('errors') or [])}")
    print("  history (last 5)      :")

    last5_statuses = []
    for r in history[:5]:
        s = r.get("status")
        last5_statuses.append(s)
        print(f"    - {s}  start={r.get('startTime')}  items={r.get('itemsProcessed', 0)}")

    # Stuck: in_progress for >24h
    if last.get("status") == "inProgress":
        start = last.get("startTime") or ""
        try:
            from datetime import datetime as _dt
            start_dt = _dt.fromisoformat(start.rstrip("Z")).replace(tzinfo=UTC)
            age = datetime.now(UTC) - start_dt
            hours = age.total_seconds() / 3600
            if hours > 24:
                print(f"\nSTUCK: indexer has been in 'inProgress' for {hours:.1f}h", file=sys.stderr)
                return 2
            print(f"\n  in_progress for {hours:.1f}h -- below 24h threshold")
        except Exception:
            print("  could not parse startTime; skipping stuck check")

    # Stuck: 5 most recent runs are all failures
    if len(last5_statuses) >= 5 and all(
        s in ("transientFailure", "persistentFailure", "error") for s in last5_statuses
    ):
        print("\nSTUCK: last 5 indexer runs all failed", file=sys.stderr)
        return 2

    print("\nIndexer healthy.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="deploy.config.json")
    ap.add_argument(
        "--coverage",
        action="store_true",
        help="Compare blob PDFs against index — print done / partial / not-started",
    )
    ap.add_argument(
        "--write-status",
        action="store_true",
        help="Persist coverage results to Cosmos DB (requires --coverage)",
    )
    ap.add_argument(
        "--triggered-by",
        default="manual",
        help="Run-record label (jenkins-cron, jenkins-manual, manual, ...)",
    )
    ap.add_argument(
        "--check-stuck-indexer",
        action="store_true",
        help="Probe the indexer status. Exit 2 if the indexer has been "
             "'inProgress' for >24h or has reported only failures over the "
             "last 5 runs (likely stuck on a bad PDF, needs reset).",
    )
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    endpoint = cfg["search"]["endpoint"].rstrip("/")
    prefix = cfg["search"].get("artifactPrefix") or "mm-manuals"
    index_name = f"{prefix}-index"

    token = DefaultAzureCredential().get_token("https://search.windows.net/.default").token
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    base = f"{endpoint}/indexes/{index_name}/docs/search?api-version={API_VERSION}"

    print(f"Index: {index_name}")
    print(f"Endpoint: {endpoint}\n")

    if args.coverage:
        coverage_report(
            cfg, endpoint, index_name, headers,
            write_status=args.write_status,
            triggered_by=args.triggered_by,
        )
        return
    if args.write_status:
        print("--write-status requires --coverage. Pass both.", file=sys.stderr)
        sys.exit(2)

    if args.check_stuck_indexer:
        rc = check_stuck_indexer(endpoint, prefix, headers)
        sys.exit(rc)

    # 1. Total document count
    resp = httpx.post(base, json={"search": "*", "top": 0, "count": True}, headers=headers, timeout=30)
    total = resp.json().get("@odata.count", "?")
    print(f"Total documents in index: {total}\n")

    # 2. Count by record_type
    print("--- Record type breakdown ---")
    for rt in ["text", "diagram", "table", "summary"]:
        body = {
            "search": "*",
            "filter": f"record_type eq '{rt}'",
            "top": 0,
            "count": True,
        }
        resp = httpx.post(base, json=body, headers=headers, timeout=30)
        count = resp.json().get("@odata.count", 0)
        print(f"  {rt:10s}: {count}")

    # 3. Sample a text record
    print("\n--- Sample text record ---")
    body = {
        "search": "*",
        "filter": "record_type eq 'text'",
        "top": 1,
        "select": "chunk_id,record_type,source_file,header_1,header_2,printed_page_label,figure_ref,physical_pdf_page,chunk,processing_status",
    }
    resp = httpx.post(base, json=body, headers=headers, timeout=30)
    hits = resp.json().get("value", [])
    if hits:
        print(json.dumps(hits[0], indent=2, ensure_ascii=False)[:1500])
    else:
        print("  (no text records found)")

    # 4. Sample a diagram record
    print("\n--- Sample diagram record ---")
    body = {
        "search": "*",
        "filter": "record_type eq 'diagram'",
        "top": 1,
        "select": "chunk_id,record_type,source_file,diagram_description,diagram_category,figure_ref,has_diagram,image_hash,processing_status",
    }
    resp = httpx.post(base, json=body, headers=headers, timeout=30)
    hits = resp.json().get("value", [])
    if hits:
        print(json.dumps(hits[0], indent=2, ensure_ascii=False)[:1500])
    else:
        print("  (no diagram records found)")

    # 5. Sample a table record
    print("\n--- Sample table record ---")
    body = {
        "search": "*",
        "filter": "record_type eq 'table'",
        "top": 1,
        "select": "chunk_id,record_type,source_file,header_1,table_caption,table_row_count,table_col_count,processing_status",
    }
    resp = httpx.post(base, json=body, headers=headers, timeout=30)
    hits = resp.json().get("value", [])
    if hits:
        print(json.dumps(hits[0], indent=2, ensure_ascii=False)[:1500])
    else:
        print("  (no table records found)")

    # 6. Sample a summary record
    print("\n--- Sample summary record ---")
    body = {
        "search": "*",
        "filter": "record_type eq 'summary'",
        "top": 1,
        "select": "chunk_id,record_type,source_file,chunk,processing_status",
    }
    resp = httpx.post(base, json=body, headers=headers, timeout=30)
    hits = resp.json().get("value", [])
    if hits:
        print(json.dumps(hits[0], indent=2, ensure_ascii=False)[:1500])
    else:
        print("  (no summary records found)")

    # 7. Check for figure_ref cross-references (text chunks with figure refs)
    print("\n--- Text chunks with figure_ref ---")
    body = {
        "search": "*",
        "filter": "record_type eq 'text' and figure_ref ne null and figure_ref ne ''",
        "top": 3,
        "select": "chunk_id,source_file,figure_ref,printed_page_label",
        "count": True,
    }
    resp = httpx.post(base, json=body, headers=headers, timeout=30)
    data = resp.json()
    count = data.get("@odata.count", 0)
    print(f"  Text chunks with figure_ref: {count}")
    for hit in data.get("value", [])[:3]:
        print(f"    {hit.get('figure_ref')} — {hit.get('source_file')} pg {hit.get('printed_page_label')}")

    # 8. Check processing_status distribution
    print("\n--- Processing status check ---")
    for status in ["ok", "cache_hit", "skipped_decorative", "no_image", "no_content", "no_source_path"]:
        body = {
            "search": "*",
            "filter": f"processing_status eq '{status}'",
            "top": 0,
            "count": True,
        }
        resp = httpx.post(base, json=body, headers=headers, timeout=30)
        count = resp.json().get("@odata.count", 0)
        if count:
            print(f"  {status:25s}: {count}")

    print("\nDone.")


if __name__ == "__main__":
    main()
