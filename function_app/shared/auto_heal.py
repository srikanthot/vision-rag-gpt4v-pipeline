"""
Auto-heal stuck blobs in the Azure Search index.

Periodically scans the index for "stuck" PDFs — blobs in the source
container that have no `summary` record after being eligible for
processing for some grace period. When found, this module:
  1. Bumps the blob's metadata (forces lastModified to advance) so the
     indexer treats it as a freshly modified blob
  2. Calls the indexer's resetdocs API to clear failed-items state for
     just those blobs
  3. Triggers an immediate indexer run

This is the production-grade replacement for manually running
`scripts/force_reindex_blobs.ps1`. It runs every 30 min via a timer
trigger in function_app.py.

Configuration (App Settings):
  AUTO_HEAL_ENABLED                  -- "true" to enable (default: true)
  AUTO_HEAL_STUCK_AFTER_MIN          -- minutes a blob must be stuck before healing (default: 60)
  AUTO_HEAL_MAX_BLOBS_PER_RUN        -- safety cap; don't try to heal more than N at once (default: 20)
  SEARCH_ENDPOINT                    -- e.g. https://srch-foo.search.windows.net
  SEARCH_INDEX_NAME                  -- e.g. mangostechmanuals-v01-index
  SEARCH_INDEXER_NAME                -- e.g. mangostechmanuals-v01-indexer
  STORAGE_ACCOUNT_NAME               -- e.g. samangosmandev01
  STORAGE_CONTAINER_NAME             -- e.g. techmanualsv07

All Azure calls use the function app's managed identity. No keys needed.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .config import feature_enabled, optional_env
from .credentials import SEARCH_SCOPE, STORAGE_SCOPE, bearer_token

_SEARCH_API_VERSION = "2024-05-01-preview"
_STORAGE_API_VERSION = "2024-08-04"


def _is_enabled() -> bool:
    # AUTO_HEAL_ENABLED must be set to "true"/"1"/"yes" to enable.
    # If unset OR set to anything else (including "false"), auto-heal stays off.
    # We intentionally default to OFF rather than ON so a fresh deploy doesn't
    # immediately start touching blobs before the operator has verified the
    # rest of the system is healthy.
    val = (optional_env("AUTO_HEAL_ENABLED", "") or "").strip().lower()
    return val in ("true", "1", "yes", "on")


def _stuck_threshold_min() -> int:
    try:
        return max(5, int(optional_env("AUTO_HEAL_STUCK_AFTER_MIN", "60") or "60"))
    except ValueError:
        return 60


def _max_blobs_per_run() -> int:
    try:
        return max(1, int(optional_env("AUTO_HEAL_MAX_BLOBS_PER_RUN", "20") or "20"))
    except ValueError:
        return 20


def _list_done_source_files(search_endpoint: str, index_name: str) -> set[str]:
    """Return source_file values that have a `summary` record in the index."""
    token = bearer_token(SEARCH_SCOPE)
    url = f"{search_endpoint.rstrip('/')}/indexes/{index_name}/docs/search?api-version={_SEARCH_API_VERSION}"
    body = {
        "search": "*",
        "filter": "record_type eq 'summary'",
        "select": "source_file",
        "top": 1000,
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=30.0) as c:
        resp = c.post(url, json=body, headers=headers)
    if resp.status_code != 200:
        logging.warning("auto_heal: index query failed: %d %s",
                        resp.status_code, resp.text[:200])
        return set()
    data = resp.json()
    return {h.get("source_file") for h in data.get("value", []) if h.get("source_file")}


def _list_pdfs_in_container(storage_account: str, container: str) -> list[dict[str, Any]]:
    """List all .pdf blobs in the container with their lastModified timestamps.

    Returns list of dicts {name, last_modified} sorted by name.
    Uses AAD-authenticated REST: GET /<container>?restype=container&comp=list&include=metadata.
    """
    endpoint_suffix = "blob.core.windows.net" if ".azure.com" in (
        optional_env("SEARCH_ENDPOINT", "") or ""
    ) else "blob.core.windows.net"
    base = f"https://{storage_account}.{endpoint_suffix}"
    url = (f"{base}/{container}?restype=container&comp=list"
           f"&include=metadata&maxresults=5000")
    token = bearer_token(STORAGE_SCOPE)
    headers = {
        "Authorization": f"Bearer {token}",
        "x-ms-version": _STORAGE_API_VERSION,
        "x-ms-date": datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT"),
    }
    with httpx.Client(timeout=60.0) as c:
        resp = c.get(url, headers=headers)
    if resp.status_code != 200:
        logging.warning("auto_heal: container list failed: %d %s",
                        resp.status_code, resp.text[:200])
        return []
    # Parse XML body. Lightweight regex parse to avoid pulling in lxml.
    import re
    body = resp.text
    pdfs: list[dict[str, Any]] = []
    # Each <Blob> block contains <Name> and <Properties><Last-Modified>...</Last-Modified>
    for m in re.finditer(r"<Blob>(.*?)</Blob>", body, re.DOTALL):
        block = m.group(1)
        name_match = re.search(r"<Name>([^<]+)</Name>", block)
        lm_match = re.search(r"<Last-Modified>([^<]+)</Last-Modified>", block)
        if not name_match or not lm_match:
            continue
        name = name_match.group(1).strip()
        if not name.lower().endswith(".pdf"):
            continue
        try:
            lm = datetime.strptime(lm_match.group(1), "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=UTC)
        except ValueError:
            continue
        pdfs.append({"name": name, "last_modified": lm})
    return sorted(pdfs, key=lambda x: x["name"])


def _bump_blob_metadata(storage_account: str, container: str, blob_name: str,
                         stamp: str) -> bool:
    """Update blob metadata to force lastModified to advance."""
    endpoint_suffix = "blob.core.windows.net" if ".azure.com" in (
        optional_env("SEARCH_ENDPOINT", "") or ""
    ) else "blob.core.windows.net"
    base = f"https://{storage_account}.{endpoint_suffix}"
    # URL-encode the blob name in path
    from urllib.parse import quote
    url = f"{base}/{container}/{quote(blob_name)}?comp=metadata"
    token = bearer_token(STORAGE_SCOPE)
    headers = {
        "Authorization": f"Bearer {token}",
        "x-ms-version": _STORAGE_API_VERSION,
        "x-ms-date": datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        "x-ms-meta-auto_heal_at": stamp,
        "Content-Length": "0",
    }
    with httpx.Client(timeout=30.0) as c:
        resp = c.put(url, headers=headers)
    if resp.status_code in (200, 201):
        return True
    logging.warning("auto_heal: metadata bump failed for %s: %d %s",
                    blob_name, resp.status_code, resp.text[:200])
    return False


def _resetdocs_and_run(search_endpoint: str, indexer_name: str,
                        blob_urls: list[str]) -> None:
    """Call resetdocs to clear failed-items state, then trigger an indexer run."""
    token = bearer_token(SEARCH_SCOPE)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = search_endpoint.rstrip("/")

    # Step 1: resetdocs
    reset_url = f"{base}/indexers/{indexer_name}/resetdocs?api-version={_SEARCH_API_VERSION}"
    body = {"datasourceDocumentIds": blob_urls}
    with httpx.Client(timeout=30.0) as c:
        resp = c.post(reset_url, json=body, headers=headers)
    if resp.status_code not in (204, 200, 202):
        logging.warning("auto_heal: resetdocs failed: %d %s",
                        resp.status_code, resp.text[:200])
        # Don't return; metadata bump alone usually still triggers reprocessing

    # Step 2: trigger run
    run_url = f"{base}/indexers/{indexer_name}/run?api-version={_SEARCH_API_VERSION}"
    with httpx.Client(timeout=30.0) as c:
        resp = c.post(run_url, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code not in (202, 200):
        # 409 Conflict is common if a run is already in progress -- that's fine
        if resp.status_code == 409:
            logging.info("auto_heal: indexer already running -- new blobs will be picked up")
        else:
            logging.warning("auto_heal: indexer run trigger failed: %d %s",
                            resp.status_code, resp.text[:200])


def auto_heal_run() -> None:
    """One pass of the auto-heal loop. Safe to call repeatedly."""
    if not _is_enabled():
        logging.info("auto_heal: disabled (AUTO_HEAL_ENABLED is not truthy)")
        return

    search_endpoint = optional_env("SEARCH_ENDPOINT")
    index_name = optional_env("SEARCH_INDEX_NAME")
    indexer_name = optional_env("SEARCH_INDEXER_NAME")
    storage_account = optional_env("STORAGE_ACCOUNT_NAME")
    container = optional_env("STORAGE_CONTAINER_NAME")

    missing = [k for k, v in {
        "SEARCH_ENDPOINT": search_endpoint,
        "SEARCH_INDEX_NAME": index_name,
        "SEARCH_INDEXER_NAME": indexer_name,
        "STORAGE_ACCOUNT_NAME": storage_account,
        "STORAGE_CONTAINER_NAME": container,
    }.items() if not v]
    if missing:
        logging.warning("auto_heal: missing app settings: %s -- skipping", missing)
        return

    stuck_after = _stuck_threshold_min()
    max_blobs = _max_blobs_per_run()
    cutoff = datetime.now(UTC) - timedelta(minutes=stuck_after)

    logging.info("auto_heal: scanning -- stuck_after=%d min, max_blobs=%d",
                 stuck_after, max_blobs)

    # 1. List source_files that have a summary record (= fully done)
    done = _list_done_source_files(search_endpoint, index_name)
    logging.info("auto_heal: index has summary records for %d PDFs", len(done))

    # 2. List blobs in container
    pdfs = _list_pdfs_in_container(storage_account, container)
    logging.info("auto_heal: container has %d PDFs", len(pdfs))

    # 3. Find blobs without summary record AND old enough to be considered stuck
    stuck = [p for p in pdfs if p["name"] not in done and p["last_modified"] < cutoff]
    if not stuck:
        logging.info("auto_heal: no stuck blobs to heal (all done or recently uploaded)")
        return

    # Cap blobs per run to avoid runaway behavior
    stuck = stuck[:max_blobs]
    logging.warning("auto_heal: %d blob(s) stuck for >= %d min -- healing",
                    len(stuck), stuck_after)
    for s in stuck:
        logging.warning("auto_heal:   - %s (last_modified=%s)",
                        s["name"], s["last_modified"].isoformat())

    # 4. Bump metadata on each stuck blob
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    bumped: list[str] = []
    endpoint_suffix = "blob.core.windows.net" if ".azure.com" in (search_endpoint or "") else "blob.core.windows.net"
    for s in stuck:
        if _bump_blob_metadata(storage_account, container, s["name"], stamp):
            bumped.append(s["name"])
            time.sleep(0.1)  # gentle pacing on storage

    if not bumped:
        logging.warning("auto_heal: 0 blobs updated -- aborting before resetdocs/run")
        return

    # 5. resetdocs + run
    blob_urls = [f"https://{storage_account}.{endpoint_suffix}/{container}/{n}" for n in bumped]
    _resetdocs_and_run(search_endpoint, indexer_name, blob_urls)

    logging.info("auto_heal: triggered re-processing of %d blobs", len(bumped))
