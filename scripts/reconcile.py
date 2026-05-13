"""
Reconcile blob storage state with the search index.

Detects three cases per PDF:
  - ADDED   : in blob, not in index           -> no action (preanalyze + indexer pick it up next run)
  - DELETED : in index, not in blob           -> purge index records + cache blobs + Cosmos state
  - EDITED  : blob's last_modified > last_indexed_at  (per Cosmos pdf_state)
                                              -> purge index records + cache blobs (forces full reanalyse)
  - STABLE  : nothing changed                 -> skip

Safety:
  - --dry-run prints what would happen without acting.
  - --max-purges (default 2) caps the number of PDFs we'll purge in one
    run. If more candidates are found, the script aborts with a clear
    message. The operator raises the cap intentionally once they trust
    the script.

Usage:
    python scripts/reconcile.py --config deploy.config.json --dry-run
    python scripts/reconcile.py --config deploy.config.json --max-purges 5
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential

# Make function_app/shared importable (parent_id derivation).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "function_app"))
from shared.ids import parent_id_for  # noqa: E402

# Local script imports.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cosmos_writer  # noqa: E402
from preanalyze import (  # noqa: E402
    _account_name,
    _init_storage,
    delete_blob,
    list_cache_blobs,
)

API_VERSION = "2024-05-01-preview"
SEARCH_SCOPE = "https://search.windows.net/.default"


@dataclass
class ReconcilePlan:
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    edited: list[str] = field(default_factory=list)
    stable: list[str] = field(default_factory=list)
    blob_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    index_chunks_per_pdf: dict[str, int] = field(default_factory=dict)


# ---------- helpers ----------

def _aad_token() -> str:
    return DefaultAzureCredential().get_token(SEARCH_SCOPE).token


def _list_blob_metadata(cfg: dict) -> dict[str, dict[str, Any]]:
    """Returns {filename: {last_modified: iso, content_length: int}} for
    every PDF blob at the container root. Uses the same az CLI path as
    preanalyze.list_pdfs, but with --query that captures last_modified."""
    import os
    import subprocess

    _init_storage(cfg)
    container = cfg["storage"]["pdfContainerName"]
    from preanalyze import _get_connection_string  # local import — same module
    conn_str = _get_connection_string(cfg)
    az_bin = "az.cmd" if os.name == "nt" else "az"
    raw = subprocess.run(
        [
            az_bin, "storage", "blob", "list",
            "--container-name", container,
            "--connection-string", conn_str,
            "--num-results", "*",
            "--query", "[].{name:name, last_modified:properties.lastModified, length:properties.contentLength}",
            "-o", "json",
        ],
        capture_output=True, text=True, check=True,
    )
    items = json.loads(raw.stdout)
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        name = item.get("name") or ""
        if not name.lower().endswith(".pdf"):
            continue
        out[name] = {
            "last_modified": item.get("last_modified"),
            "content_length": item.get("length") or 0,
        }
    return out


def _query_index_pdfs(endpoint: str, index_name: str, headers: dict) -> dict[str, int]:
    """Returns {source_file: chunk_count} via facet query on the index."""
    url = f"{endpoint}/indexes/{index_name}/docs/search?api-version={API_VERSION}"
    body = {"search": "*", "facet": "source_file,count:0", "top": 0}
    with httpx.Client(timeout=60.0) as c:
        resp = c.post(url, json=body, headers=headers)
    resp.raise_for_status()
    facets = resp.json().get("@odata.facets", {}).get("source_file", [])
    return {f["value"]: int(f.get("count", 0)) for f in facets if f.get("value")}


def _delete_index_records_for_parent(
    endpoint: str,
    index_name: str,
    headers: dict,
    parent_id: str,
    max_keys_per_batch: int = 1000,
) -> int:
    """
    Delete every record where parent_id eq '<parent_id>'. Returns the
    number of records deleted.

    Implementation: search for chunk_id keys filtered by parent_id, page
    through them, and POST a batch of @search.action: delete to the
    /docs/index endpoint.
    """
    deleted_total = 0
    search_url = f"{endpoint}/indexes/{index_name}/docs/search?api-version={API_VERSION}"
    index_url = f"{endpoint}/indexes/{index_name}/docs/index?api-version={API_VERSION}"

    while True:
        body = {
            "search": "*",
            "filter": f"parent_id eq '{parent_id}'",
            "select": "chunk_id,id",
            "top": max_keys_per_batch,
        }
        with httpx.Client(timeout=60.0) as c:
            resp = c.post(search_url, json=body, headers=headers)
        resp.raise_for_status()
        hits = resp.json().get("value", [])
        if not hits:
            break
        actions = []
        for h in hits:
            # The index key is `id` per index.json. Some records may also
            # carry chunk_id; we delete by `id` to match the schema.
            key = h.get("id") or h.get("chunk_id")
            if not key:
                continue
            actions.append({"@search.action": "delete", "id": key})
        if not actions:
            break
        with httpx.Client(timeout=60.0) as c:
            del_resp = c.post(index_url, json={"value": actions}, headers=headers)
        del_resp.raise_for_status()
        # Count successful deletions; failures stay in the index but we
        # continue (operator can retry).
        results = del_resp.json().get("value", [])
        success_n = sum(1 for r in results if r.get("status"))
        deleted_total += success_n
        if len(hits) < max_keys_per_batch:
            break
    return deleted_total


def _delete_cache_blobs_for_pdf(cfg: dict, pdf_name: str) -> int:
    """Delete every _dicache/<pdf_name>.* blob. Returns deleted count."""
    cache_blobs = list_cache_blobs(cfg)
    prefix = f"_dicache/{pdf_name}."
    targets = [b for b in cache_blobs if b.startswith(prefix)]
    n = 0
    for b in targets:
        try:
            if delete_blob(cfg, b):
                n += 1
        except Exception as exc:
            logging.warning("delete cache blob %s failed: %s", b, exc)
    return n


# ---------- planning ----------

def build_plan(cfg: dict, endpoint: str, index_name: str, headers: dict) -> ReconcilePlan:
    """Compare blob, index, and Cosmos pdf_state to produce a plan."""
    plan = ReconcilePlan()

    print("Listing PDFs in blob container...")
    plan.blob_meta = _list_blob_metadata(cfg)
    blob_pdfs = set(plan.blob_meta.keys())
    print(f"  {len(blob_pdfs)} PDFs in blob")

    print("Querying index for source_file facet...")
    plan.index_chunks_per_pdf = _query_index_pdfs(endpoint, index_name, headers)
    indexed_pdfs = set(plan.index_chunks_per_pdf.keys())
    print(f"  {len(indexed_pdfs)} PDFs have records in the index")

    # Read existing pdf_state from Cosmos so we know last_indexed_at per PDF.
    last_indexed_at: dict[str, str] = {}
    if cosmos_writer._is_configured(cfg):
        try:
            db = cosmos_writer._get_database_client(cfg)
            container = cosmos_writer._ensure_container(db, cosmos_writer.PDF_STATE_CONTAINER)
            for item in container.read_all_items():
                sf = item.get("source_file") or item.get("id")
                ts = item.get("last_indexed_at")
                if sf and ts:
                    last_indexed_at[sf] = ts
            print(f"  {len(last_indexed_at)} PDFs have Cosmos state rows")
        except Exception as exc:
            logging.warning("Cosmos pdf_state read failed: %s", exc)

    # Classify each PDF.
    for pdf in sorted(blob_pdfs | indexed_pdfs):
        in_blob = pdf in blob_pdfs
        in_index = pdf in indexed_pdfs
        if in_blob and not in_index:
            plan.added.append(pdf)
            continue
        if in_index and not in_blob:
            plan.deleted.append(pdf)
            continue
        # In both. Edit detection: blob.last_modified > last_indexed_at.
        blob_lm = plan.blob_meta.get(pdf, {}).get("last_modified") or ""
        cosmos_ts = last_indexed_at.get(pdf, "")
        if blob_lm and cosmos_ts and blob_lm > cosmos_ts:
            plan.edited.append(pdf)
        elif blob_lm and not cosmos_ts:
            # No Cosmos record but the PDF is already in the index.
            # Treat as stable on this run; the next preanalyze/indexer
            # cycle will populate the Cosmos state row, after which edit
            # detection works.
            plan.stable.append(pdf)
        else:
            plan.stable.append(pdf)
    return plan


# ---------- execution ----------

def execute_plan(cfg: dict, plan: ReconcilePlan, endpoint: str,
                 index_name: str, headers: dict, dry_run: bool) -> dict[str, Any]:
    """Carry out the deletes and edit-purges in the plan. Returns a stats
    dict suitable for the run record."""
    stats = {
        "deleted_pdfs": 0, "edited_pdfs": 0,
        "chunks_purged": 0, "cache_blobs_purged": 0,
        "errors": [],
    }
    container_account = _account_name(cfg)
    container_name = cfg["storage"]["pdfContainerName"]

    targets = [(pdf, "deleted") for pdf in plan.deleted] + [(pdf, "edited") for pdf in plan.edited]
    for pdf, kind in targets:
        # Recompute parent_id the same way the function app does.
        # source_path is what the indexer feeds into parent_id_for.
        source_path = (
            f"https://{container_account}.blob.core.windows.net/"
            f"{container_name}/{pdf}"
        )
        parent_id = parent_id_for(source_path, pdf)

        if dry_run:
            print(f"  [dry-run] would purge {pdf} ({kind}, parent_id={parent_id}, "
                  f"chunks={plan.index_chunks_per_pdf.get(pdf, 0)})")
            continue

        # 1. Delete index records for this parent_id.
        try:
            n = _delete_index_records_for_parent(endpoint, index_name, headers, parent_id)
            stats["chunks_purged"] += n
            print(f"  purged {n} index records for {pdf} ({kind})")
        except Exception as exc:
            err = f"index purge failed for {pdf}: {exc}"
            logging.warning(err)
            stats["errors"].append(err)
            continue

        # 2. Delete cache blobs.
        try:
            blobs = _delete_cache_blobs_for_pdf(cfg, pdf)
            stats["cache_blobs_purged"] += blobs
            print(f"  purged {blobs} cache blobs for {pdf}")
        except Exception as exc:
            err = f"cache purge failed for {pdf}: {exc}"
            logging.warning(err)
            stats["errors"].append(err)

        # 3. For deleted PDFs, also clear Cosmos state.
        if kind == "deleted":
            cosmos_writer.delete_pdf_state(cfg, pdf)
            stats["deleted_pdfs"] += 1
        else:
            # For edited, leave the Cosmos row; preanalyze/check_index
            # will overwrite it on the next run with the new state.
            stats["edited_pdfs"] += 1

    return stats


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="Reconcile blob storage with search index")
    ap.add_argument("--config", default="deploy.config.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print plan without making any changes")
    ap.add_argument("--max-purges", type=int, default=2,
                    help="Refuse to purge more than N PDFs in one run (default 2)")
    ap.add_argument("--no-lock", action="store_true",
                    help="Skip the pipeline lock. Use only for read/diagnostic mode.")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    endpoint = cfg["search"]["endpoint"].rstrip("/")
    prefix = cfg["search"].get("artifactPrefix") or "mm-manuals"
    index_name = f"{prefix}-index"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_aad_token()}",
    }

    # Acquire the same pipeline lock preanalyze uses, so the two can't
    # collide (e.g. reconcile purges a cache blob preanalyze just wrote).
    # Dry-run mode doesn't take the lock — it's read-only.
    lock_id = None
    if not args.dry_run and not args.no_lock:
        try:
            from pipeline_lock import LockHeldError, acquire_lock
            lock_id = acquire_lock(cfg, "preanalyze")
            print(f"  acquired pipeline lock (id={lock_id[:8]}...)")
        except LockHeldError as exc:
            print(f"\nABORT: {exc}")
            return 2
        except Exception as exc:
            print(f"  warning: lock acquire failed: {exc}; proceeding")

    print(f"Reconcile starting (dry_run={args.dry_run}, max_purges={args.max_purges})")
    plan = build_plan(cfg, endpoint, index_name, headers)
    print()
    print(f"  ADDED   : {len(plan.added)}  (no action — preanalyze + indexer will pick them up)")
    print(f"  EDITED  : {len(plan.edited)}  (will purge index + cache, then preanalyze re-runs)")
    print(f"  DELETED : {len(plan.deleted)} (will purge index + cache + Cosmos state)")
    print(f"  STABLE  : {len(plan.stable)}")

    purge_count = len(plan.deleted) + len(plan.edited)
    if purge_count > args.max_purges and not args.dry_run:
        print()
        print(f"ABORT: {purge_count} PDFs would be purged but --max-purges is {args.max_purges}.")
        print("       Re-run with a higher --max-purges (or --dry-run to inspect first).")
        return 2

    if plan.deleted or plan.edited:
        print()
        print("Executing purges:")
        stats = execute_plan(cfg, plan, endpoint, index_name, headers, args.dry_run)
    else:
        stats = {"deleted_pdfs": 0, "edited_pdfs": 0,
                 "chunks_purged": 0, "cache_blobs_purged": 0, "errors": []}

    # Persist a run record (best-effort).
    if not args.dry_run:
        cosmos_writer.write_run_record(cfg, {
            "run_type": "reconcile",
            "triggered_by": "manual",
            "added": plan.added,
            "edited": plan.edited,
            "deleted": plan.deleted,
            "stable_count": len(plan.stable),
            "chunks_purged": stats["chunks_purged"],
            "cache_blobs_purged": stats["cache_blobs_purged"],
            "errors": stats["errors"],
        })

    # Release lock before exit.
    if lock_id is not None:
        try:
            from pipeline_lock import release_lock
            release_lock(cfg, "preanalyze", lock_id)
            print("  released pipeline lock")
        except Exception as exc:
            print(f"  warning: lock release failed: {exc}")

    print()
    print("Reconcile done.")
    if stats["errors"]:
        print(f"  WARNING: {len(stats['errors'])} errors during execution; see logs")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
