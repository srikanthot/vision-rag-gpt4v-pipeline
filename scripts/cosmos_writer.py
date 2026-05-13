"""
Cosmos DB writer for indexing pipeline run history and per-PDF state.

Two containers, both in the existing Cosmos DB account configured in
deploy.config.json:

  indexing_run_history  partition key: /partitionKey   (date string)
  indexing_pdf_state    partition key: /partitionKey   (source_file)

Used by:
  - preanalyze.py            (writes one run record at end of each invocation)
  - check_index.py           (writes one run record + per-PDF state when called
                              with --write-status)
  - run_pipeline.py          (writes one run record covering the full pipeline)
  - reconcile.py             (writes one run record summarising add/edit/delete)

All writes are best-effort: a Cosmos failure must not fail the underlying
operation. The caller's run is the source of truth; the dashboard is a
view, and a missing row just means the next run won't have a comparison
point. Errors are logged at WARNING level so the operator can still
investigate, but the script returns 0 if the underlying work succeeded.

Auth: Managed Identity via DefaultAzureCredential. The Jenkins agent (or
local user) must hold the Cosmos DB Built-in Data Contributor role on the
target account, scoped to the database.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

# Lazy imports of azure-cosmos so this module can be imported even in
# environments that don't have the dep (eg. tests). Failure to import
# converts to a no-op writer.
_cosmos_client_factory = None
_cosmos_import_error: str | None = None

try:
    from azure.cosmos import CosmosClient, PartitionKey
    from azure.cosmos import exceptions as cosmos_exceptions
    from azure.identity import DefaultAzureCredential

    _cosmos_client_factory = (CosmosClient, PartitionKey, DefaultAzureCredential, cosmos_exceptions)
except ImportError as exc:
    _cosmos_import_error = str(exc)


RUN_HISTORY_CONTAINER = "indexing_run_history"
PDF_STATE_CONTAINER = "indexing_pdf_state"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_partition() -> str:
    """Partition key for run_history: YYYY-MM-DD. Keeps one day's runs
    together for efficient time-window queries."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _is_configured(cfg: dict) -> bool:
    """Returns True iff the config has a usable cosmos block."""
    cosmos_cfg = cfg.get("cosmos") or {}
    if not cosmos_cfg.get("endpoint") or not cosmos_cfg.get("database"):
        return False
    if _cosmos_client_factory is None:
        logging.warning(
            "cosmos_writer disabled: azure-cosmos not installed (%s)",
            _cosmos_import_error,
        )
        return False
    return True


_client_cache: dict[str, Any] = {}


def _get_database_client(cfg: dict):
    """Cached database client for the configured Cosmos account.
    DefaultAzureCredential walks the same chain Function App and other
    scripts use, so behaviour is consistent."""
    cosmos_cfg = cfg["cosmos"]
    cache_key = f"{cosmos_cfg['endpoint']}::{cosmos_cfg['database']}"
    if cache_key in _client_cache:
        return _client_cache[cache_key]

    CosmosClient, _, DefaultAzureCredential, _ = _cosmos_client_factory
    cred = DefaultAzureCredential()
    client = CosmosClient(cosmos_cfg["endpoint"], credential=cred)
    db = client.get_database_client(cosmos_cfg["database"])
    _client_cache[cache_key] = db
    return db


def _ensure_container(db, container_name: str, partition_key_path: str = "/partitionKey"):
    """Ensure the container exists. Idempotent; safe to call every run.
    Uses 400 RU/s shared throughput by default at the database level —
    we don't allocate per-container throughput."""
    _, PartitionKey, _, exceptions = _cosmos_client_factory
    try:
        return db.get_container_client(container_name).read() and db.get_container_client(container_name)
    except exceptions.CosmosResourceNotFoundError:
        try:
            db.create_container(
                id=container_name,
                partition_key=PartitionKey(path=partition_key_path),
            )
            logging.info("created Cosmos container %s", container_name)
            return db.get_container_client(container_name)
        except exceptions.CosmosResourceExistsError:
            return db.get_container_client(container_name)
    except Exception:
        # Any other read error: try to use the container anyway. Cosmos
        # will surface a clearer error on the upsert.
        return db.get_container_client(container_name)


def write_run_record(cfg: dict, record: dict[str, Any]) -> bool:
    """
    Write one run-history document. Returns True on success, False on
    config-disabled or any failure (logged).

    Caller passes a dict; this function stamps id, partitionKey,
    started_at (if missing), and ended_at automatically.
    """
    if not _is_configured(cfg):
        return False

    doc = dict(record)  # shallow copy — caller dict not mutated
    doc.setdefault("ended_at", _now_iso())
    doc.setdefault("started_at", doc["ended_at"])
    doc["id"] = doc.get("id") or f"{doc['ended_at']}-{uuid.uuid4().hex[:8]}"
    doc["partitionKey"] = doc.get("partitionKey") or _today_partition()

    try:
        db = _get_database_client(cfg)
        container = _ensure_container(db, RUN_HISTORY_CONTAINER)
        container.upsert_item(doc)
        return True
    except Exception as exc:
        logging.warning("cosmos run_history upsert failed: %s", exc)
        return False


def write_pdf_state(cfg: dict, source_file: str, state: dict[str, Any]) -> bool:
    """
    Upsert one per-PDF state document. Returns True on success.

    The document id == source_file so each PDF has exactly one state
    row, replaced on every run. Use partitionKey == source_file too;
    that keeps each PDF's history in its own logical partition for
    efficient point reads from Power BI.
    """
    if not _is_configured(cfg):
        return False
    if not source_file:
        return False

    doc = dict(state)
    doc["id"] = source_file
    doc["partitionKey"] = source_file
    doc.setdefault("updated_at", _now_iso())

    try:
        db = _get_database_client(cfg)
        container = _ensure_container(db, PDF_STATE_CONTAINER)
        container.upsert_item(doc)
        return True
    except Exception as exc:
        logging.warning("cosmos pdf_state upsert for %s failed: %s",
                        source_file, exc)
        return False


def write_pdf_states_bulk(cfg: dict, states: list[dict[str, Any]]) -> int:
    """
    Upsert many per-PDF state documents. Returns the number that
    succeeded. Best-effort: a single failure does not abort the rest.

    Each item must contain a `source_file` key; that becomes id +
    partitionKey.
    """
    if not _is_configured(cfg) or not states:
        return 0
    written = 0
    try:
        db = _get_database_client(cfg)
        container = _ensure_container(db, PDF_STATE_CONTAINER)
    except Exception as exc:
        logging.warning("cosmos pdf_state bulk: container open failed: %s", exc)
        return 0
    now = _now_iso()
    for state in states:
        source_file = state.get("source_file")
        if not source_file:
            continue
        doc = dict(state)
        doc["id"] = source_file
        doc["partitionKey"] = source_file
        doc.setdefault("updated_at", now)
        try:
            container.upsert_item(doc)
            written += 1
        except Exception as exc:
            logging.warning("cosmos pdf_state upsert for %s failed: %s",
                            source_file, exc)
    return written


def delete_pdf_state(cfg: dict, source_file: str) -> bool:
    """
    Remove a per-PDF state document. Returns True on success or if the
    document didn't exist (idempotent). Used by reconcile.py when a PDF
    is deleted from the blob container.
    """
    if not _is_configured(cfg) or not source_file:
        return False
    try:
        _, _, _, exceptions = _cosmos_client_factory
        db = _get_database_client(cfg)
        container = _ensure_container(db, PDF_STATE_CONTAINER)
        try:
            container.delete_item(item=source_file, partition_key=source_file)
        except exceptions.CosmosResourceNotFoundError:
            pass  # already gone, fine
        return True
    except Exception as exc:
        logging.warning("cosmos pdf_state delete for %s failed: %s",
                        source_file, exc)
        return False
