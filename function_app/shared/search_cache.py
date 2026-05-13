"""
Image-hash cache lookup against the existing index.

Skips re-running the vision model if a previous indexing run already
processed an identical image (same parent_id + image_hash).

Hardening:
- OData string-literal escaping to prevent filter injection (and to
  survive any unexpected characters in parent_id or image_hash).
- Field whitelist (`SELECT_FIELDS`) keeps the lookup robust to schema
  evolution: only fields known to exist in the current index are read.
- Feature-gated: silently no-ops if SEARCH_ENDPOINT is not configured.
- 429 retry with Retry-After honoured (capped) — Search rate limits
  used to silently kill cache hits during peak indexing.
"""

from __future__ import annotations

import logging
import re
import time
from functools import lru_cache
from typing import Any

import httpx

from .config import feature_enabled, optional_env, required_env
from .credentials import SEARCH_SCOPE, bearer_token, use_managed_identity

# Fields the cache reads back from a previous successful diagram record.
# Whitelisted explicitly so this never tries to SELECT a field that has
# been removed from the index schema.
SELECT_FIELDS = [
    "diagram_description",
    "diagram_category",
    "figure_ref",
    "has_diagram",
    "surrounding_context",
    "header_1",
    "header_2",
    "header_3",
    "figure_bbox",
    "figure_id",
]

# Acceptable values for the filter inputs. Anything outside this regex
# is rejected before it touches the OData string. Hashes are hex; parent
# ids come from _short_hash which is also hex; figure ids are alnum +
# '_' + '-'.
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _odata_escape(value: str) -> str:
    """Escape an OData string literal: single quotes are doubled."""
    return (value or "").replace("'", "''")


def _safe_token(value: str) -> str | None:
    if not value:
        return None
    if SAFE_TOKEN_RE.match(value):
        return value
    return None


def _enabled() -> bool:
    if not optional_env("SEARCH_ENDPOINT"):
        return False
    if not optional_env("SEARCH_INDEX_NAME"):
        # Refuse to silently target the wrong index. If the operator forgot
        # to set SEARCH_INDEX_NAME, every cache lookup would silently miss
        # and we'd re-pay every vision call for the lifetime of the deploy.
        # The previous default ("mm-manuals-index") was a footgun.
        logging.warning(
            "search_cache disabled: SEARCH_INDEX_NAME is not set. "
            "Wire it through your Function App App Settings."
        )
        return False
    if use_managed_identity():
        return True
    return feature_enabled("SEARCH_ENDPOINT", "SEARCH_ADMIN_KEY")


def _auth_header() -> dict[str, str]:
    if use_managed_identity():
        return {"Authorization": f"Bearer {bearer_token(SEARCH_SCOPE)}"}
    key = optional_env("SEARCH_ADMIN_KEY")
    if not key:
        return {}
    return {"api-key": key}


@lru_cache(maxsize=1)
def _index_url() -> str:
    endpoint = optional_env("SEARCH_ENDPOINT").rstrip("/")
    # required_env() raises if absent, but _enabled() is already gating
    # this so we wouldn't get here with the env var unset.
    index_name = required_env("SEARCH_INDEX_NAME")
    return f"{endpoint}/indexes/{index_name}/docs/search?api-version=2024-05-01-preview"


def _post_with_429_retry(url: str, json_body: dict, headers: dict[str, str],
                          timeout_s: float = 5.0,
                          max_retries: int = 1) -> httpx.Response | None:
    """POST that retries on HTTP 429 with Retry-After.

    Tightened defaults (was timeout=10s, max_retries=2): worst-case
    wall clock = 2 attempts × (5s timeout + 15s Retry-After) = 40s,
    well within the 230s skill budget even when chained with a phash
    lookup. The previous defaults gave 120s per call × 2 calls (sha256
    + phash) = 240s, BLOWING the 230s timeout BEFORE the vision call
    even started -- a direct explanation for "0-doc cycles when
    Search index was throttling".

    Cache lookups are an OPTIMIZATION (skip vision on hit). If Search
    is throttling, the right move is fail-fast and run vision; that
    keeps the indexer making progress at the cost of duplicate vision
    spend, instead of pinning workers on retry sleep."""
    # Use the shared httpx client from di_client for connection pooling.
    # Was creating a new client per call here (TLS handshake every time),
    # which compounded the search_cache's already-slow 240s worst-case
    # backoff scenario.
    from .di_client import _SHARED_CLIENT
    attempt = 0
    while True:
        try:
            resp = _SHARED_CLIENT.post(url, json=json_body, headers=headers, timeout=timeout_s)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            if attempt >= max_retries:
                logging.warning("hash cache POST network error after %d retries: %s",
                                attempt, exc)
                return None
            attempt += 1
            time.sleep(min(1.0 * (2 ** attempt), 10.0))
            continue
        if resp.status_code != 429:
            return resp
        if attempt >= max_retries:
            return resp
        retry_after = resp.headers.get("Retry-After", "")
        try:
            wait_s = float(retry_after) if retry_after else 2.0 * (2 ** attempt)
        except ValueError:
            wait_s = 2.0 * (2 ** attempt)
        # Cap wait at 15s (was 30s). Worst-case wall clock per call now
        # 2 × (5s timeout + 15s wait) = 40s. See timeout/max_retries doc.
        wait_s = min(max(wait_s, 1.0), 15.0)
        logging.info("search_cache POST 429, sleeping %.1fs", wait_s)
        time.sleep(wait_s)
        attempt += 1


def lookup_existing_by_hash(parent_id: str, image_hash: str) -> dict[str, Any] | None:
    """
    Find a previous diagram record with the same parent + image hash that
    completed successfully. Returns the cached fields, or None.

    Returns None (not raises) on:
      - feature disabled (env vars not set)
      - empty/invalid inputs
      - any HTTP or parsing failure (logged at warning)
    """
    if not _enabled():
        return None
    if not parent_id or not image_hash or image_hash == "noimage":
        return None

    safe_parent = _safe_token(parent_id)
    safe_hash = _safe_token(image_hash)
    if not safe_parent or not safe_hash:
        logging.warning(
            "hash cache lookup skipped: unsafe token (parent=%r, hash=%r)",
            parent_id, image_hash,
        )
        return None

    body = {
        "search": "*",
        "filter": (
            f"record_type eq 'diagram' "
            f"and dgm_parent_id eq '{_odata_escape(safe_parent)}' "
            f"and image_hash eq '{_odata_escape(safe_hash)}' "
            f"and processing_status eq 'ok'"
        ),
        "select": ",".join(SELECT_FIELDS),
        "top": 1,
    }
    headers = {"Content-Type": "application/json", **_auth_header()}

    resp = _post_with_429_retry(_index_url(), body, headers, timeout_s=5.0)
    if resp is None:
        return None
    if resp.status_code != 200:
        logging.warning(
            "hash cache lookup failed: %s %s",
            resp.status_code, resp.text[:200],
        )
        return None
    try:
        hits = resp.json().get("value", [])
        return hits[0] if hits else None
    except Exception as exc:
        logging.warning("hash cache parse error: %s", exc)
        return None


def lookup_existing_by_phash(image_phash: str) -> dict[str, Any] | None:
    """Cross-PDF figure dedup via perceptual hash. Looks up any prior
    diagram record across the entire index whose image_phash matches.

    Gated by env flag SEARCH_CACHE_CROSS_PARENT=true (default off).
    Reason: it changes dedup semantics — a shared OEM nameplate
    appearing in 50 different manuals collapses to ONE vision call
    instead of 50, but it also means a "cached" record's caption /
    surrounding_context come from a *different* manual, which can be
    semantically misleading when the same image carries different
    contextual meaning. Only enable after evaluating false-positive
    rate on your corpus.

    Returns None (not raises) on every failure mode. Callers should
    fall through to the live vision call when None is returned.
    """
    if not _enabled():
        return None
    if not feature_enabled("SEARCH_CACHE_CROSS_PARENT"):
        return None
    if not image_phash:
        return None

    safe_phash = _safe_token(image_phash)
    if not safe_phash:
        return None

    body = {
        "search": "*",
        "filter": (
            f"record_type eq 'diagram' "
            f"and image_phash eq '{_odata_escape(safe_phash)}' "
            f"and processing_status eq 'ok'"
        ),
        "select": ",".join(SELECT_FIELDS),
        "top": 1,
    }
    headers = {"Content-Type": "application/json", **_auth_header()}
    resp = _post_with_429_retry(_index_url(), body, headers, timeout_s=5.0)
    if resp is None:
        return None
    if resp.status_code != 200:
        logging.warning(
            "phash cache lookup failed: %s %s",
            resp.status_code, resp.text[:200],
        )
        return None
    try:
        hits = resp.json().get("value", [])
        return hits[0] if hits else None
    except Exception as exc:
        logging.warning("phash cache parse error: %s", exc)
        return None
