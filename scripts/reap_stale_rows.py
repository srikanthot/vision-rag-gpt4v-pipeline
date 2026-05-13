"""
Reap stale rows from the search index.

Production reality: text chunk_id includes layout_ordinal + sha1(chunk)[:12].
Any change to DI version, SplitSkill window size, or our text_utils
normalization produces NEW chunk_ids on re-index. The old rows are not
overwritten — they sit in the index forever as near-duplicates that
rank against the new content.

This script deletes rows that meet BOTH of these conditions:
  - skill_version != current SKILL_VERSION
  - last_indexed_at < (now - keep_days)

Both conditions are required to avoid deleting rows that just haven't
been re-projected yet on a slow indexer run. Skill-version drift alone
isn't enough — a row with the previous version that landed in the index
yesterday could be the one currently being replaced and we'd be racing
the indexer.

Run AFTER a successful full reindex. Default keep_days=7 is a safe
buffer for any indexer pass that's been completed within a week.

Usage:
    # Dry-run: show counts without deleting
    python scripts/reap_stale_rows.py --config deploy.config.json --dry-run

    # Actually delete (requires explicit --yes)
    python scripts/reap_stale_rows.py --config deploy.config.json --yes

    # Tighten keep_days for an aggressive purge
    python scripts/reap_stale_rows.py --config deploy.config.json --keep-days 1 --yes
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def _az_bin() -> str:
    return "az.cmd" if os.name == "nt" else "az"


def _get_search_token() -> str:
    """Acquire a bearer token for the Azure Search data plane via az CLI."""
    r = subprocess.run(
        [_az_bin(), "account", "get-access-token", "--resource",
         "https://search.windows.net", "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        log.error("az get-access-token failed: %s", r.stderr.strip())
        sys.exit(2)
    return r.stdout.strip()


def _read_skill_version(repo_root: Path) -> str:
    """Read the current SKILL_VERSION from function_app/shared/ids.py.
    The reaper has to know the in-flight version to know what NOT to
    delete; reading from the source-of-truth file avoids drift."""
    ids_path = repo_root / "function_app" / "shared" / "ids.py"
    text = ids_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("SKILL_VERSION"):
            # Format: SKILL_VERSION = "v3"  (or with comment)
            value = line.split("=", 1)[1].strip()
            return value.strip("\"' ")
    log.error("Could not find SKILL_VERSION in %s", ids_path)
    sys.exit(2)


def _build_filter(skill_version: str, keep_days: int) -> str:
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=keep_days)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    # OData filter — the index has skill_version filterable and
    # last_indexed_at filterable+sortable.
    return (
        f"skill_version ne '{skill_version}' "
        f"and last_indexed_at lt {cutoff_iso}"
    )


def _post(endpoint: str, path: str, body: dict, token: str) -> httpx.Response:
    url = f"{endpoint.rstrip('/')}{path}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    return httpx.post(url, headers=headers, json=body, timeout=60)


def _search_for_stale(endpoint: str, index_name: str, token: str,
                      filter_expr: str, top: int) -> list[dict]:
    """Return up to `top` rows matching the stale-row filter."""
    body = {
        "search": "*",
        "filter": filter_expr,
        "select": "chunk_id,record_type,skill_version,last_indexed_at",
        "top": top,
    }
    path = f"/indexes/{urllib.parse.quote(index_name)}/docs/search?api-version=2024-11-01-preview"
    resp = _post(endpoint, path, body, token)
    if resp.status_code != 200:
        log.error("search failed: %s %s", resp.status_code, resp.text[:300])
        sys.exit(1)
    return resp.json().get("value", [])


def _delete_rows(endpoint: str, index_name: str, token: str,
                 chunk_ids: list[str]) -> int:
    """Delete rows by chunk_id via the index's docs endpoint. Batched at
    100 to stay under the per-call payload limit."""
    deleted = 0
    path = f"/indexes/{urllib.parse.quote(index_name)}/docs/index?api-version=2024-11-01-preview"
    for i in range(0, len(chunk_ids), 100):
        batch = chunk_ids[i:i + 100]
        body = {
            "value": [
                {"@search.action": "delete", "chunk_id": cid}
                for cid in batch
            ]
        }
        resp = _post(endpoint, path, body, token)
        if resp.status_code not in (200, 207):
            log.error("delete batch %d-%d failed: %s %s",
                      i, i + len(batch), resp.status_code, resp.text[:300])
            continue
        # 207 (multi-status) means some sub-operations may have failed;
        # we report the whole batch as 'attempted' but log non-2xx items.
        try:
            data = resp.json()
            ok = sum(1 for r in data.get("value", []) if r.get("status"))
            deleted += ok
        except Exception:
            deleted += len(batch)
    return deleted


def main() -> int:
    ap = argparse.ArgumentParser(description="Delete stale index rows after reindex")
    ap.add_argument("--config", default="deploy.config.json")
    ap.add_argument("--keep-days", type=int, default=7,
                    help="Don't delete rows indexed within the last N days "
                         "(safety buffer against racing the indexer; default 7)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print counts and a sample without deleting")
    ap.add_argument("--yes", action="store_true",
                    help="Confirm deletion (required when not --dry-run)")
    ap.add_argument("--max-rows", type=int, default=10000,
                    help="Cap total rows deleted in one run (default 10000)")
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    endpoint = cfg["search"]["endpoint"].rstrip("/")
    prefix = cfg["search"].get("artifactPrefix") or "mm-manuals"
    index_name = f"{prefix}-index"

    skill_version = _read_skill_version(repo_root)
    filter_expr = _build_filter(skill_version, args.keep_days)
    log.info("Current SKILL_VERSION = %s", skill_version)
    log.info("Filter: %s", filter_expr)
    log.info("Index:  %s", index_name)

    token = _get_search_token()

    # Page through results, deleting in chunks.
    total_seen = 0
    total_deleted = 0
    while total_seen < args.max_rows:
        batch_size = min(1000, args.max_rows - total_seen)
        rows = _search_for_stale(endpoint, index_name, token, filter_expr, batch_size)
        if not rows:
            break
        total_seen += len(rows)
        chunk_ids = [r["chunk_id"] for r in rows if r.get("chunk_id")]

        if args.dry_run:
            log.info("[dry-run] would delete %d stale rows (sample below):", len(chunk_ids))
            for r in rows[:5]:
                log.info("  %s | %s | skill=%s | last=%s",
                         r.get("chunk_id"), r.get("record_type"),
                         r.get("skill_version"), r.get("last_indexed_at"))
            break  # one page is enough for dry-run

        if not args.yes:
            log.error("Refusing to delete without --yes flag")
            return 1

        deleted = _delete_rows(endpoint, index_name, token, chunk_ids)
        total_deleted += deleted
        log.info("Deleted %d rows (running total: %d)", deleted, total_deleted)

        # If the index returned fewer than batch_size, we've drained the
        # eligible set. Bail out.
        if len(rows) < batch_size:
            break

    log.info("Done. Stale rows seen: %d. Deleted: %d.", total_seen, total_deleted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
