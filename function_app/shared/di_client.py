"""
Azure Document Intelligence REST client.

Calls the prebuilt-layout model directly so we can access the structured
figures[] and tables[] arrays that the built-in DocumentIntelligenceLayoutSkill
does not surface. Built-in skill remains in the skillset for the markdown
text path; this client is for the figure/table enrichment path.

Pre-analysis cache: if a .di.json file exists alongside the PDF in blob
storage, we read that instead of calling DI live. This removes the 230-second
Azure Search WebApi skill timeout constraint for large PDFs. Run
scripts/preanalyze.py before the indexer to populate the cache.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .config import optional_env, required_env
from .credentials import (
    DI_SCOPE,
    STORAGE_SCOPE,
    bearer_token,
    use_managed_identity,
)

# Module-level shared HTTP client. Without this, every fetch_cached_*
# call constructs a fresh httpx.Client(), paying TCP + TLS handshake on
# each invocation (~50-150ms). For a PDF with 200 figures × 2 fetches
# (crop + vision), that's 20-60s of pure handshake overhead per doc.
# Shared client keeps the connection pool warm: subsequent requests on
# the same host reuse the TLS session. Timeout is per-request (set on
# .get/.post call), not on the client.
_SHARED_CLIENT = httpx.Client(
    timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0),
    # 50 connections handles a worker servicing ~50 parallel skill calls,
    # each potentially doing 2-3 blob GETs. 20 was too low; pool exhaustion
    # under load caused PoolTimeout on the 21st+ caller. 50 gives headroom
    # without bloating per-worker memory significantly (~1MB).
    limits=httpx.Limits(max_connections=50, max_keepalive_connections=25),
)


def _endpoint() -> str:
    return required_env("DI_ENDPOINT").rstrip("/")


def _api_version() -> str:
    return optional_env("DI_API_VERSION", "2024-11-30")


def _auth_headers() -> dict[str, str]:
    """
    Header set for DI requests. MI path uses a bearer token; key path uses
    Ocp-Apim-Subscription-Key. DI supports both natively.
    """
    if use_managed_identity():
        return {"Authorization": f"Bearer {bearer_token(DI_SCOPE)}"}
    return {"Ocp-Apim-Subscription-Key": required_env("DI_API_KEY")}


def analyze_layout(pdf_bytes: bytes, timeout_s: int = 210) -> dict[str, Any]:
    """
    Submit a PDF to the prebuilt-layout model and poll until the result is ready.
    Returns the full analyzeResult payload (pages, paragraphs, sections,
    figures, tables, ...).
    """
    url = (
        f"{_endpoint()}/documentintelligence/documentModels/prebuilt-layout:analyze"
        f"?api-version={_api_version()}&outputContentFormat=markdown"
    )
    auth = _auth_headers()
    headers = {**auth, "Content-Type": "application/pdf"}

    submit = _SHARED_CLIENT.post(url, headers=headers, content=pdf_bytes, timeout=180.0)
    if submit.status_code not in (200, 202):
        raise RuntimeError(
            f"DI submit failed: {submit.status_code} {submit.text[:500]}"
        )
    op_loc = submit.headers.get("operation-location")
    if not op_loc:
        raise RuntimeError("DI submit missing operation-location header")

    deadline = time.time() + timeout_s
    backoff = 2.0
    while time.time() < deadline:
        # Re-fetch the auth header each poll so MI token refreshes are picked up.
        # (bearer_token is now cached with TTL so this is a cheap dict lookup.)
        poll = _SHARED_CLIENT.get(op_loc, headers=_auth_headers(), timeout=30.0)
        if poll.status_code != 200:
            raise RuntimeError(
                f"DI poll failed: {poll.status_code} {poll.text[:500]}"
            )
        body = poll.json()
        status = body.get("status")
        if status == "succeeded":
            return body.get("analyzeResult", {})
        if status == "failed":
            raise RuntimeError(f"DI analyze failed: {body}")
        time.sleep(backoff)
        backoff = min(backoff * 1.25, 8.0)

    raise TimeoutError("DI analyze timed out")


def _split_blob_url(blob_url: str) -> tuple[str, str] | None:
    """
    Split an https blob URL into (base, decoded_filename), preserving
    proper URL encoding for the cache lookups built off it. Returns None
    if the URL has no path segment.

    blob_url comes in from the indexer as `metadata_storage_path`, which
    Azure Search emits already URL-encoded. We rebuild cache URLs by
    swapping the filename for `_dicache/<filename>.<suffix>`, so we need
    to be careful to keep the encoded form on the wire while operating
    on the decoded form internally.
    """
    parts = urlsplit(blob_url)
    if not parts.path:
        return None
    last_slash = parts.path.rfind("/")
    if last_slash < 0:
        return None
    # The path is URL-encoded; we leave it that way for `base` so the
    # rebuilt URL stays valid, and we don't actually use the decoded
    # filename for reconstruction — quote() handles that on the way out.
    base_path = parts.path[: last_slash + 1]
    base = urlunsplit((parts.scheme, parts.netloc, base_path, "", ""))
    # Strip trailing slash so callers can append "<dir>/<name>" cleanly.
    if base.endswith("/"):
        base = base[:-1]
    encoded_filename = parts.path[last_slash + 1 :]
    return base, encoded_filename


def _build_cache_url(blob_url: str, suffix: str) -> str | None:
    """
    Build a `<base>/_dicache/<filename>.<suffix>` URL with proper URL
    encoding on the filename. Returns None if blob_url is malformed.

    Critical: the input blob_url's filename is already URL-encoded
    (Azure Search emits it that way), so we use it as-is in the cache
    URL. We never decode-then-reencode here because that risks double-
    encoding for filenames that legitimately contain '%' characters.
    """
    parts = _split_blob_url(blob_url)
    if not parts:
        return None
    base, encoded_filename = parts
    return f"{base}/_dicache/{encoded_filename}.{suffix}"


def _build_cache_url_with_id(blob_url: str, prefix: str, id_value: str) -> str | None:
    """
    Build `<base>/_dicache/<filename>.<prefix>.<id>.json` for per-figure
    cache blobs (crops, vision results). Encodes id_value defensively.
    """
    parts = _split_blob_url(blob_url)
    if not parts:
        return None
    base, encoded_filename = parts
    safe_id = quote(id_value or "", safe="")
    return f"{base}/_dicache/{encoded_filename}.{prefix}.{safe_id}.json"


def _storage_auth_headers() -> dict[str, str]:
    """Auth headers for blob fetches. MI path uses bearer; otherwise
    relies on a SAS token appended to the URL by callers."""
    if use_managed_identity():
        return {
            "Authorization": f"Bearer {bearer_token(STORAGE_SCOPE)}",
            "x-ms-version": "2023-11-03",
        }
    return {}


def _apply_sas_if_needed(url: str) -> str:
    """If MI is disabled and STORAGE_BLOB_SAS is set, append it. Idempotent."""
    if use_managed_identity():
        return url
    sas = optional_env("STORAGE_BLOB_SAS").lstrip("?")
    if sas and "?" not in url:
        return f"{url}?{sas}"
    return url


def _http_get_with_retry(url: str, headers: dict[str, str], timeout_s: float,
                         max_retries: int = 3) -> httpx.Response | None:
    """
    GET helper that retries on HTTP 429 (rate-limited). On other transient
    errors (timeout, connection reset) we let the caller decide; this helper
    is used by cache lookups where None on miss is normal.

    Returns the Response on terminal status (any 2xx, 404, 4xx other than
    429), or None if all retries exhausted.
    """
    attempt = 0
    while True:
        try:
            resp = _SHARED_CLIENT.get(url, headers=headers, timeout=timeout_s)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            if attempt >= max_retries:
                logging.warning("blob GET network error after %d retries: %s", attempt, exc)
                return None
            attempt += 1
            time.sleep(min(1.0 * (2 ** attempt), 10.0))
            continue

        if resp.status_code != 429:
            return resp
        if attempt >= max_retries:
            logging.warning("blob GET 429 after %d retries: %s", max_retries, url)
            return resp
        retry_after = resp.headers.get("Retry-After", "")
        try:
            wait_s = float(retry_after) if retry_after else 2.0 * (2 ** attempt)
        except ValueError:
            wait_s = 2.0 * (2 ** attempt)
        wait_s = min(max(wait_s, 1.0), 30.0)
        logging.info("blob GET 429, sleeping %.1fs before retry", wait_s)
        time.sleep(wait_s)
        attempt += 1


def fetch_blob_bytes(blob_url: str) -> bytes:
    """
    Fetch a blob over HTTPS. The url passed in by the indexer is
    metadata_storage_path -- the unauthenticated blob URL.

    Auth priority:
      1. Managed identity (production). Requires the Function App's MI to
         have the 'Storage Blob Data Reader' role on the account.
      2. SAS token in STORAGE_BLOB_SAS (useful for local dev when MI is
         not available).
      3. Bare URL (blob must be public-read).
    """
    headers = _storage_auth_headers()
    fetch_url = _apply_sas_if_needed(blob_url)

    resp = _SHARED_CLIENT.get(fetch_url, headers=headers, timeout=180.0)
    if resp.status_code != 200:
        raise RuntimeError(
            f"blob fetch failed: {resp.status_code} {resp.text[:200]}"
        )
    return resp.content


def fetch_cached_analysis(blob_url: str) -> dict[str, Any] | None:
    """
    Check if a pre-analyzed DI result exists in the _dicache/ subfolder.
    The cache blob path is: <container>/_dicache/<filename>.pdf.di.json

    Returns {"analyzeResult": {...}} on hit, or None on miss.
    Crops are stored as separate per-figure blobs and fetched on demand
    via fetch_cached_crop(). This keeps memory usage low even for PDFs
    with thousands of figures.

    Run scripts/preanalyze.py to populate the cache for large PDFs.
    """
    cache_url = _build_cache_url(blob_url, "di.json")
    if not cache_url:
        logging.warning("fetch_cached_analysis: could not build cache_url from blob_url=%s", blob_url)
        return None
    fetch_url = _apply_sas_if_needed(cache_url)
    headers = _storage_auth_headers()
    logging.info("fetch_cached_analysis: GET %s (auth_header=%s)",
                 cache_url, "yes" if headers.get("Authorization") else "no")

    try:
        resp = _http_get_with_retry(fetch_url, headers, timeout_s=20.0)
        if resp is None:
            logging.warning("fetch_cached_analysis: _http_get_with_retry returned None for %s", cache_url)
            return None
        logging.info("fetch_cached_analysis: status=%d for %s", resp.status_code, cache_url)
        if resp.status_code == 200:
            logging.info("DI cache hit: %s (size=%d bytes)", cache_url, len(resp.content))
            data = json.loads(resp.content)
            # Support both new format (bare analyzeResult) and old
            # v2 wrapper format ({"analyzeResult": ..., "crops": ...})
            if isinstance(data, dict) and "analyzeResult" in data:
                return {"analyzeResult": data["analyzeResult"]}
            return {"analyzeResult": data}
        if resp.status_code == 404:
            return None
        # Loud about every other status: it indicates a real failure
        # (auth, permissions, throttling) the operator needs to know
        # about, NOT a cache miss. Returning None is correct (fall back
        # to live DI), but the warning surfaces the underlying issue.
        logging.warning(
            "DI cache fetch unexpected status %d for %s: %s",
            resp.status_code, cache_url, resp.text[:200],
        )
        return None
    except json.JSONDecodeError as exc:
        logging.warning("DI cache JSON decode failed for %s: %s", cache_url, exc)
        return None
    except Exception as exc:
        logging.warning("DI cache fetch error for %s: %s", cache_url, exc)
        return None


def fetch_cached_crop(blob_url: str, figure_id: str) -> dict[str, Any] | None:
    """
    Fetch a single pre-cropped figure from the _dicache/ subfolder.
    Blob path: <container>/_dicache/<filename>.pdf.crop.<figure_id>.json

    Returns {"image_b64": "...", "bbox": {...}} on hit, or None on miss.
    """
    crop_url = _build_cache_url_with_id(blob_url, "crop", figure_id)
    if not crop_url:
        return None
    fetch_url = _apply_sas_if_needed(crop_url)
    headers = _storage_auth_headers()

    try:
        resp = _http_get_with_retry(fetch_url, headers, timeout_s=30.0)
        if resp is None:
            return None
        if resp.status_code == 200:
            return json.loads(resp.content)
        if resp.status_code == 404:
            return None
        logging.warning(
            "crop cache fetch unexpected status %d for %s/%s: %s",
            resp.status_code, blob_url, figure_id, resp.text[:200],
        )
        return None
    except json.JSONDecodeError as exc:
        logging.warning("crop cache JSON decode failed for %s/%s: %s",
                        blob_url, figure_id, exc)
        return None
    except Exception as exc:
        logging.warning("crop cache error for %s/%s: %s",
                        blob_url, figure_id, exc)
        return None


def fetch_cached_sections(blob_url: str) -> list[dict[str, Any]] | None:
    """
    Fetch pre-built section_index from sidecar blob.
    Blob path: <container>/_dicache/<filename>.pdf.sections.json

    Returns the section list on hit, None on miss. The section_index is
    expensive to build at function-app runtime for huge PDFs (3-5 min
    on documents with 2700+ sections, blowing past the 230s WebApi skill
    timeout). preanalyze.py builds and uploads this sidecar so the skill
    chain loads pre-built data in ~1 sec instead.
    """
    sections_url = _build_cache_url(blob_url, "sections.json")
    if not sections_url:
        return None
    fetch_url = _apply_sas_if_needed(sections_url)
    headers = _storage_auth_headers()

    try:
        resp = _http_get_with_retry(fetch_url, headers, timeout_s=20.0)
        if resp is None:
            return None
        if resp.status_code == 200:
            logging.info("sections sidecar hit: %s (size=%d bytes)",
                         sections_url, len(resp.content))
            return json.loads(resp.content)
        if resp.status_code == 404:
            return None
        logging.warning(
            "sections sidecar fetch unexpected status %d for %s: %s",
            resp.status_code, sections_url, resp.text[:200],
        )
        return None
    except json.JSONDecodeError as exc:
        logging.warning("sections sidecar JSON decode failed for %s: %s",
                        sections_url, exc)
        return None
    except Exception as exc:
        logging.warning("sections sidecar fetch error for %s: %s",
                        sections_url, exc)
        return None


def fetch_precomputed_output(blob_url: str) -> dict[str, Any] | None:
    """
    Check if a pre-computed process-document output exists.
    Blob path: <container>/_dicache/<filename>.pdf.output.json

    Returns the full output dict on hit, or None on miss.
    """
    output_url = _build_cache_url(blob_url, "output.json")
    if not output_url:
        return None
    fetch_url = _apply_sas_if_needed(output_url)
    headers = _storage_auth_headers()

    try:
        resp = _http_get_with_retry(fetch_url, headers, timeout_s=20.0)
        if resp is None:
            return None
        if resp.status_code == 200:
            # Defensive size cap. output.json should be metadata only
            # (paths, bboxes, captions, section text) -- typical size
            # is 1-5 MB. A 200 MB file would block the worker for 5-15s
            # in json.loads alone, eating budget the 230s skill timeout
            # depends on. Refuse to parse oversized blobs and emit a
            # loud error so the operator knows to inspect output.json.
            body_size = len(resp.content)
            if body_size > 50_000_000:
                logging.error(
                    "pre-computed output too large (%d bytes > 50 MB) for %s. "
                    "Re-run preanalyze.py --phase output --force; output.json "
                    "should not contain image_b64 bodies.",
                    body_size, output_url,
                )
                return None
            logging.info("pre-computed output hit: %s (%d bytes)", output_url, body_size)
            return json.loads(resp.content)
        if resp.status_code == 404:
            return None
        logging.warning(
            "pre-computed output unexpected status %d for %s: %s",
            resp.status_code, output_url, resp.text[:200],
        )
        return None
    except json.JSONDecodeError as exc:
        logging.warning("pre-computed output JSON decode failed for %s: %s",
                        output_url, exc)
        return None
    except Exception as exc:
        logging.warning("pre-computed output error for %s: %s", output_url, exc)
        return None


def fetch_precomputed_vision(blob_url: str, figure_id: str) -> dict[str, Any] | None:
    """
    Fetch pre-computed vision analysis for a single figure.
    Blob path: <container>/_dicache/<filename>.pdf.vision.<figure_id>.json

    Returns the vision result dict on hit, or None on miss.
    """
    vision_url = _build_cache_url_with_id(blob_url, "vision", figure_id)
    if not vision_url:
        return None
    fetch_url = _apply_sas_if_needed(vision_url)
    headers = _storage_auth_headers()

    try:
        resp = _http_get_with_retry(fetch_url, headers, timeout_s=30.0)
        if resp is None:
            return None
        if resp.status_code == 200:
            logging.info("pre-computed vision hit: %s/%s", blob_url, figure_id)
            return json.loads(resp.content)
        if resp.status_code == 404:
            return None
        logging.warning(
            "pre-computed vision unexpected status %d for %s/%s: %s",
            resp.status_code, blob_url, figure_id, resp.text[:200],
        )
        return None
    except json.JSONDecodeError as exc:
        logging.warning("pre-computed vision JSON decode for %s/%s failed: %s",
                        blob_url, figure_id, exc)
        return None
    except Exception as exc:
        logging.warning("pre-computed vision error for %s/%s: %s",
                        blob_url, figure_id, exc)
        return None
