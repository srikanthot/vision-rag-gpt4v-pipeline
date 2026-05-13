"""
Pre-analyze PDFs with Document Intelligence and cache results in blob.
Run BEFORE the indexer. The process-document skill reads cached results
instead of calling DI live, removing the 230-second WebApi skill timeout
constraint for large PDFs.

Features:
  - Image triage: skips tiny/decorative figures (< 1.0" or < 10KB PNG
    or extreme aspect ratio)
  - Hash dedup: skips identical crops within each PDF
  - Vision pre-analysis: calls GPT-4 vision during preanalyze so the
    indexer returns precomputed results instantly
  - Intra-PDF parallelism: runs N vision calls concurrently per PDF
  - Resumable phases: --phase di / vision / output so crashes don't
    lose progress
  - Incremental mode: --incremental skips PDFs that already have output
  - Cleanup mode: --cleanup removes orphaned cache for deleted PDFs

Usage:
    # Full run (all phases, sequential)
    python scripts/preanalyze.py --config deploy.config.json

    # Phased run (resumable)
    python scripts/preanalyze.py --config deploy.config.json --phase di --concurrency 3
    python scripts/preanalyze.py --config deploy.config.json --phase vision --vision-parallel 20
    python scripts/preanalyze.py --config deploy.config.json --phase output

    # Incremental (only new PDFs, for scheduled automation)
    python scripts/preanalyze.py --config deploy.config.json --incremental

    # Cleanup orphaned cache
    python scripts/preanalyze.py --config deploy.config.json --cleanup

    # Force re-process everything
    python scripts/preanalyze.py --config deploy.config.json --force --vision-parallel 20
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx

# -- Vision API helpers --

_aoai_key_cache: str | None = None
_aoai_key_lock = threading.Lock()
_di_key_lock = threading.Lock()
_conn_str_lock = threading.Lock()
_storage_init_lock = threading.Lock()

FIGURE_REF_RE = re.compile(
    r"\b(Figure|Fig\.?)\s*[\-:]?\s*([A-Z]{0,3}[\-\.]?\d[\w\-\.]{0,8})",
    re.IGNORECASE,
)

VISION_SYSTEM_PROMPT = """You are a technical-manual diagram analyst.

Return STRICT JSON with these keys:
  category:    one of [circuit_diagram, wiring_diagram, schematic, line_diagram, block_diagram, pid_diagram, flow_diagram, control_logic, exploded_view, parts_list_diagram, nameplate, equipment_photo, decorative, unknown]
  is_useful:   boolean. true unless category is decorative/unknown.
  figure_ref:  e.g. "Figure 4-2", "Fig. 12", or "" if none visible.
  description: dense, retrieval-friendly description (3-8 sentences).
               For diagrams, name components, labels, connections, units, and
               what the diagram is showing. For nameplates, transcribe key
               fields. If any text or value is unclear, say so explicitly.
               Do not guess.
  ocr_text:    transcribe ALL visible text labels, part numbers, values,
               wire tags, terminal IDs, model numbers, and callout numbers
               found in the image. Preserve the original text exactly.
               Separate items with " | ". If no readable text, return "".

Return ONLY the JSON object. No markdown, no commentary."""

USEFUL_CATEGORIES = {
    "circuit_diagram", "wiring_diagram", "schematic", "line_diagram",
    "block_diagram", "pid_diagram", "flow_diagram", "control_logic",
    "exploded_view", "parts_list_diagram", "nameplate", "equipment_photo",
}

# Minimum vision description length in characters. Below this, we treat
# the response as degenerate (truncated / hallucinated) and retry once.
# 30 is enough to capture short useful descriptions like "Wiring diagram
# showing breaker B1, B2, B3 connections to busbar TB-1." but rejects
# fragments like "Diagram." or "(unclear)". Bar lowered from 30 to 20
# so brief-but-legitimate equipment-photo descriptions ("Side view of
# relay 5L. Cover removed.") aren't forced into a retry.
_VISION_MIN_DESCRIPTION_CHARS = 20


def _validate_and_retry_if_degenerate(
    cfg: dict,
    image_b64: str,
    user_text: str,
    vision_result: dict,
    fig_id: str,
) -> dict:
    """If the vision result is structurally fine but the description is
    suspiciously short (and the figure is supposed to be useful), retry
    once with a stricter instruction. Accepts whatever comes back on
    retry — we don't loop indefinitely."""
    if not isinstance(vision_result, dict):
        return vision_result
    category = (vision_result.get("category") or "").strip().lower()
    description = (vision_result.get("description") or "").strip()
    is_useful = bool(vision_result.get("is_useful"))

    if not is_useful or category in ("decorative", "unknown"):
        return vision_result  # legitimately short is OK
    if len(description) >= _VISION_MIN_DESCRIPTION_CHARS:
        return vision_result

    print(f"    vision degenerate ({fig_id}, len={len(description)}); retrying once",
          flush=True)
    stricter = (
        user_text
        + "\n\nIMPORTANT: your previous description was too short. "
          "Provide a 3-8 sentence dense description naming components, "
          "labels, connections, and the diagram's purpose."
    )
    try:
        retry_result = _call_vision_api(cfg, image_b64, stricter, max_retries=1)
        if isinstance(retry_result, dict):
            new_desc = (retry_result.get("description") or "").strip()
            if len(new_desc) >= _VISION_MIN_DESCRIPTION_CHARS:
                return retry_result
    except Exception as exc:
        print(f"    vision retry failed ({fig_id}): {exc}", flush=True)
    return vision_result  # accept the degenerate one rather than blocking

# -- Image triage thresholds (v3 -- more aggressive) --
MIN_WIDTH_IN = 1.0
MIN_HEIGHT_IN = 1.0
MIN_CROP_BYTES = 10_000   # 10 KB
MAX_ASPECT_RATIO = 8.0    # skip horizontal lines / vertical dividers


def _get_aoai_key(cfg: dict) -> str:
    global _aoai_key_cache
    if _aoai_key_cache is not None:
        return _aoai_key_cache
    with _aoai_key_lock:
        if _aoai_key_cache is not None:
            return _aoai_key_cache
        ep = cfg["azureOpenAI"]["endpoint"].rstrip("/")
        resource_name = ep.split("//")[1].split(".")[0]
        rg = cfg["functionApp"]["resourceGroup"]
        raw = _run_az([
            "az", "cognitiveservices", "account", "keys", "list",
            "--name", resource_name, "--resource-group", rg, "-o", "json",
        ])
        _aoai_key_cache = json.loads(raw)["key1"]
        return _aoai_key_cache


def _call_vision_api(cfg: dict, image_b64: str, user_text: str, max_retries: int = 3) -> dict:
    ep = cfg["azureOpenAI"]["endpoint"].rstrip("/")
    deployment = cfg["azureOpenAI"]["visionDeployment"]
    api_ver = cfg["azureOpenAI"].get("apiVersion", "2024-12-01-preview")
    api_key = _get_aoai_key(cfg)
    url = f"{ep}/openai/deployments/{deployment}/chat/completions?api-version={api_ver}"

    body = {
        "messages": [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            ]},
        ],
        "temperature": 0.0,
        "max_tokens": 1500,
        "response_format": {"type": "json_object"},
    }
    headers = {"api-key": api_key, "Content-Type": "application/json"}

    ssl_verify: str | bool = os.environ.get("SSL_CERT_FILE") or True
    for attempt in range(max_retries):
        try:
            with httpx.Client(timeout=60.0, verify=ssl_verify) as client:
                resp = client.post(url, json=body, headers=headers)
                if resp.status_code == 429:
                    # Cap Retry-After at 120s so a misconfigured server
                    # can't make us block for an hour on a single figure.
                    try:
                        retry_after = int(resp.headers.get("Retry-After", "10"))
                    except (TypeError, ValueError):
                        retry_after = 10
                    retry_after = min(max(retry_after, 1), 120)
                    print(f"        rate limited, waiting {retry_after}s...", flush=True)
                    time.sleep(retry_after)
                    continue
                if resp.status_code != 200:
                    # 4xx (content filter, bad request, auth) is not retryable.
                    # Only retry 5xx server errors.
                    if resp.status_code >= 500 and attempt < max_retries - 1:
                        time.sleep(5)
                        continue
                    raise RuntimeError(f"vision API {resp.status_code}: {resp.text[:300]}")
                # Parse the response. If the payload is malformed we treat
                # it as a transient failure and retry, because AOAI has been
                # known to return truncated chunked-transfer bodies.
                try:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                    if attempt < max_retries - 1:
                        time.sleep(5)
                        continue
                    raise RuntimeError(f"vision API malformed response: {exc}") from exc
                text = (content or "").strip()
                if text.startswith("```"):
                    text = re.sub(r"^```(?:json)?", "", text).rstrip("`").strip()
                return json.loads(text)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError):
            if attempt < max_retries - 1:
                time.sleep(5)
                continue
            raise
    return {}


def _build_vision_user_text(fig_data: dict) -> str:
    source_file = fig_data.get("source_file", "")
    h1 = fig_data.get("header_1", "")
    h2 = fig_data.get("header_2", "")
    h3 = fig_data.get("header_3", "")
    header_path = " > ".join([h for h in [h1, h2, h3] if h])
    page = str(fig_data.get("page_number", ""))
    caption = fig_data.get("caption", "")
    surrounding = fig_data.get("surrounding_context", "")

    refs = ", ".join(
        sorted(set(f"{m.group(1).title()} {m.group(2)}" for m in FIGURE_REF_RE.finditer(surrounding)))
    )
    surrounding_safe = surrounding[:1500].replace('"', "'")

    return (
        f'You are analyzing a figure from technical manual "{source_file}".\n'
        f"Section: {header_path or '(unknown)'}\n"
        f"Page: {page or '(unknown)'}\n"
        f"Caption (from layout): {caption or '(none)'}\n"
        f"Body text references this figure as: {refs or '(none)'}\n"
        f'Surrounding text: "{surrounding_safe}"\n\n'
        f"If this is a technical diagram, describe it in full detail.\n"
        f"If any text/value is unclear, say so explicitly. Do not guess.\n"
        f"If decorative/logo/photo, return category=decorative and is_useful=false."
    )


# -- Config + storage helpers --

def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"config file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _account_name(cfg: dict) -> str:
    parts = cfg["storage"]["accountResourceId"].rstrip("/").split("/")
    return parts[-1]


_conn_str_cache: str | None = None


def _run_az(cmd: list[str]) -> str:
    if cmd and cmd[0] == "az":
        cmd[0] = "az.cmd" if os.name == "nt" else "az"
    r = subprocess.run(cmd, capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        print(f"  az CLI error (exit {r.returncode}):", flush=True)
        print(f"  stderr: {r.stderr[:500]}", flush=True)
        print(f"  stdout: {r.stdout[:500]}", flush=True)
        r.check_returncode()
    return r.stdout.strip()


def _get_connection_string(cfg: dict) -> str:
    global _conn_str_cache
    if _conn_str_cache is not None:
        return _conn_str_cache
    with _conn_str_lock:
        if _conn_str_cache is not None:
            return _conn_str_cache
        account = _account_name(cfg)
        rg = cfg["functionApp"]["resourceGroup"]
        raw = _run_az([
            "az", "storage", "account", "show-connection-string",
            "--name", account, "--resource-group", rg, "-o", "json",
        ])
        _conn_str_cache = json.loads(raw)["connectionString"]
        return _conn_str_cache


def _parse_conn_str(conn_str: str) -> dict[str, str]:
    parts = {}
    for pair in conn_str.split(";"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            parts[k] = v
    return parts


_storage_client: httpx.Client | None = None
_storage_account_name: str = ""
_storage_endpoint_suffix: str = ""
_storage_credential = None
_storage_token: str = ""
_storage_token_expiry: float = 0.0


def _init_storage(cfg: dict) -> None:
    """Initialize HTTP client + AAD credential for storage. Uses AAD bearer
    auth (via DefaultAzureCredential) instead of shared-key HMAC signing,
    so it works when "Allow shared key access" is disabled on the storage
    account (Azure security baseline default in many orgs) AND when the
    deploy principal has data-plane RBAC but no key-listing permission.
    The user's `az login` token is picked up automatically.
    """
    global _storage_client, _storage_account_name, _storage_endpoint_suffix
    global _storage_credential
    if _storage_client is not None:
        return
    with _storage_init_lock:
        if _storage_client is not None:
            return
        _storage_account_name = _account_name(cfg)
        # Endpoint suffix for the cloud (.windows.net for Gov, .net for commercial).
        # Derive from the storage accountResourceId path; default to commercial.
        rid = cfg["storage"].get("accountResourceId", "") or ""
        if "windows" in rid or ".azure.com" in str(cfg.get("search", {}).get("endpoint", "")):
            _storage_endpoint_suffix = "core.windows.net"
        else:
            _storage_endpoint_suffix = "core.windows.net"
        from azure.identity import DefaultAzureCredential
        _storage_credential = DefaultAzureCredential()
        ssl_verify: str | bool = os.environ.get("SSL_CERT_FILE") or True
        _storage_client = httpx.Client(
            timeout=httpx.Timeout(connect=30.0, write=600.0, read=600.0, pool=30.0),
            verify=ssl_verify,
        )


def _get_storage_token() -> str:
    """Fetch (and cache) an AAD bearer token for Azure Storage. Token is
    valid for 1 hour; refresh when within 60 sec of expiry. Thread-safe
    via _storage_init_lock (acquired only on refresh)."""
    global _storage_token, _storage_token_expiry
    import time
    now = time.time()
    if _storage_token and _storage_token_expiry > now + 60:
        return _storage_token
    with _storage_init_lock:
        if _storage_token and _storage_token_expiry > now + 60:
            return _storage_token
        # Storage AAD scope. Same for commercial and Gov clouds -- the
        # endpoint differs (login.microsoftonline.com vs .com) but the
        # scope literal is identical. DefaultAzureCredential picks the
        # right authority from the user's az login context.
        scope = "https://storage.azure.com/.default"
        tok = _storage_credential.get_token(scope)
        _storage_token = tok.token
        _storage_token_expiry = float(tok.expires_on)
        return _storage_token


def _storage_auth_header(method: str, container: str, blob_name: str,
                          content_length: int = 0,
                          content_type: str = "",
                          extra_headers: dict[str, str] | None = None) -> dict[str, str]:
    """Build AAD bearer auth headers for a storage REST call. Signature
    kept for backward compatibility with all callers; the `method`,
    `container`, `blob_name`, `content_length`, `content_type`,
    `extra_headers` args are no longer used (AAD doesn't require
    request-specific signing)."""
    return {
        "Authorization": f"Bearer {_get_storage_token()}",
        "x-ms-version": "2023-11-03",
    }


def _blob_url(container: str, name: str) -> str:
    # quote(safe="/") preserves slashes for nested paths (e.g. "_dicache/foo.pdf")
    # while percent-encoding spaces and other URL-unsafe characters in the
    # blob name. Without this, blob names containing spaces (a real-world
    # case in customer-supplied PDFs) generate URLs that httpx may pass
    # through unencoded, producing an HTTP 403 from Azure Storage that
    # masquerades as a permissions error.
    encoded = quote(name, safe="/")
    return f"https://{_storage_account_name}.blob.{_storage_endpoint_suffix}/{container}/{encoded}"


# Supported file extensions (lowercase). DI's prebuilt-layout natively
# handles all four; PyMuPDF cropping only works on .pdf, so non-PDF files
# get text + tables only (no figure crops or vision).
SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".pptx", ".xlsx")


def _is_pdf(name: str) -> bool:
    return name.lower().endswith(".pdf")


def list_pdfs(cfg: dict) -> list[str]:
    """List supported document blobs at the container root.

    Despite the name (kept for backwards-compat with check_index.py and
    reconcile.py) this lists all SUPPORTED_EXTENSIONS, not only PDFs.
    Cache blobs under _dicache/ are excluded.

    Note: `az storage blob list` caps at 5000 results by default. Once
    _dicache/ accumulates thousands of crop/vision blobs, the lexicographic
    page can cut off lowercase-named PDFs that sort AFTER '_dicache/'.
    `--num-results *` overrides the cap.
    """
    _init_storage(cfg)
    container = cfg["storage"]["pdfContainerName"]
    conn_str = _get_connection_string(cfg)
    raw = _run_az([
        "az", "storage", "blob", "list",
        "--container-name", container,
        "--connection-string", conn_str,
        "--num-results", "*",
        "--query", "[].name",
        "-o", "json",
    ])
    all_names = json.loads(raw)
    return [
        n for n in all_names
        if n.lower().endswith(SUPPORTED_EXTENSIONS)
        and not n.startswith("_dicache/")
    ]


def list_cache_blobs(cfg: dict) -> list[str]:
    """List all blobs in _dicache/ prefix. Needs --num-results *: a single
    large PDF can produce thousands of crop + vision blobs, well past the
    5000-result default cap."""
    _init_storage(cfg)
    container = cfg["storage"]["pdfContainerName"]
    conn_str = _get_connection_string(cfg)
    raw = _run_az([
        "az", "storage", "blob", "list",
        "--container-name", container,
        "--connection-string", conn_str,
        "--prefix", "_dicache/",
        "--num-results", "*",
        "--query", "[].name",
        "-o", "json",
    ])
    return json.loads(raw)


_BLOB_RETRY_ATTEMPTS = 3
_BLOB_RETRY_BASE_DELAY = 2.0
# Cap on the Retry-After value we'll honor from the server. Avoids a
# malicious or mis-configured Retry-After header stalling preanalyze for
# minutes per blob.
_BLOB_RETRY_AFTER_CAP = 30.0


def _retry_after_seconds(resp: "httpx.Response | None", default_s: float) -> float:
    """Parse the server's Retry-After hint; cap to _BLOB_RETRY_AFTER_CAP.
    Falls back to `default_s` when absent or unparseable."""
    if resp is None:
        return default_s
    ra = resp.headers.get("Retry-After", "") if hasattr(resp, "headers") else ""
    if not ra:
        return default_s
    try:
        return min(max(float(ra), 1.0), _BLOB_RETRY_AFTER_CAP)
    except (TypeError, ValueError):
        return default_s


def blob_exists(cfg: dict, name: str) -> bool:
    """HEAD a blob. Returns True if 200, False if 404. Raises on network
    errors or unexpected status codes so we never silently treat a
    transient failure as 'blob missing' (which would trigger duplicate work).
    """
    _init_storage(cfg)
    container = cfg["storage"]["pdfContainerName"]
    url = _blob_url(container, name)

    last_exc: Exception | None = None
    for attempt in range(_BLOB_RETRY_ATTEMPTS):
        resp = None
        try:
            headers = _storage_auth_header("HEAD", container, name)
            resp = _storage_client.head(url, headers=headers)
            if resp.status_code == 200:
                return True
            if resp.status_code == 404:
                return False
            # Treat 5xx / throttling as transient; retry
            if resp.status_code >= 500 or resp.status_code == 429:
                last_exc = RuntimeError(f"blob HEAD {resp.status_code}: {resp.text[:120]}")
            else:
                raise RuntimeError(f"blob HEAD unexpected {resp.status_code}: {resp.text[:200]}")
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
        if attempt < _BLOB_RETRY_ATTEMPTS - 1:
            time.sleep(_retry_after_seconds(resp, _BLOB_RETRY_BASE_DELAY * (attempt + 1)))
    raise RuntimeError(f"blob HEAD failed after {_BLOB_RETRY_ATTEMPTS} attempts: {last_exc}")


def fetch_blob(cfg: dict, name: str) -> bytes:
    _init_storage(cfg)
    container = cfg["storage"]["pdfContainerName"]
    url = _blob_url(container, name)

    last_exc: Exception | None = None
    for attempt in range(_BLOB_RETRY_ATTEMPTS):
        resp = None
        try:
            headers = _storage_auth_header("GET", container, name)
            resp = _storage_client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.content
            if resp.status_code >= 500 or resp.status_code == 429:
                last_exc = RuntimeError(f"blob GET {resp.status_code}: {resp.text[:120]}")
            else:
                raise RuntimeError(f"blob fetch failed: {resp.status_code} {resp.text[:200]}")
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
        if attempt < _BLOB_RETRY_ATTEMPTS - 1:
            time.sleep(_retry_after_seconds(resp, _BLOB_RETRY_BASE_DELAY * (attempt + 1)))
    raise RuntimeError(f"blob GET failed after {_BLOB_RETRY_ATTEMPTS} attempts: {last_exc}")


def upload_blob(cfg: dict, name: str, data: bytes) -> None:
    _init_storage(cfg)
    container = cfg["storage"]["pdfContainerName"]
    url = _blob_url(container, name)
    content_type = "application/json"

    last_exc: Exception | None = None
    for attempt in range(_BLOB_RETRY_ATTEMPTS):
        resp = None
        try:
            extra = {"x-ms-blob-type": "BlockBlob"}
            headers = _storage_auth_header("PUT", container, name,
                                             content_length=len(data),
                                             content_type=content_type,
                                             extra_headers=extra)
            headers["Content-Type"] = content_type
            headers["x-ms-blob-type"] = "BlockBlob"
            resp = _storage_client.put(url, headers=headers, content=data)
            if resp.status_code in (200, 201):
                return
            if resp.status_code >= 500 or resp.status_code == 429:
                last_exc = RuntimeError(f"blob PUT {resp.status_code}: {resp.text[:120]}")
            else:
                raise RuntimeError(f"blob upload failed: {resp.status_code} {resp.text[:200]}")
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
        if attempt < _BLOB_RETRY_ATTEMPTS - 1:
            time.sleep(_retry_after_seconds(resp, _BLOB_RETRY_BASE_DELAY * (attempt + 1)))
    raise RuntimeError(f"blob PUT failed after {_BLOB_RETRY_ATTEMPTS} attempts: {last_exc}")


def delete_blob(cfg: dict, name: str) -> bool:
    _init_storage(cfg)
    container = cfg["storage"]["pdfContainerName"]
    url = _blob_url(container, name)
    headers = _storage_auth_header("DELETE", container, name)
    resp = _storage_client.request("DELETE", url, headers=headers)
    return resp.status_code in (200, 202, 204)


# -- DI helpers --

_di_key_cache: str | None = None


def _get_di_key(cfg: dict) -> str:
    global _di_key_cache
    if _di_key_cache is not None:
        return _di_key_cache
    with _di_key_lock:
        if _di_key_cache is not None:
            return _di_key_cache
        ep = cfg["documentIntelligence"]["endpoint"].rstrip("/")
        resource_name = ep.split("//")[1].split(".")[0]
        rg = cfg["functionApp"]["resourceGroup"]
        raw = _run_az([
            "az", "cognitiveservices", "account", "keys", "list",
            "--name", resource_name, "--resource-group", rg, "-o", "json",
        ])
        _di_key_cache = json.loads(raw)["key1"]
        return _di_key_cache


def _generate_blob_sas(cfg: dict, blob_name: str, expiry_minutes: int = 60) -> str:
    """Generate a read-only SAS URL for a blob so DI can fetch it directly."""
    _init_storage(cfg)
    container = cfg["storage"]["pdfContainerName"]
    conn_str = _get_connection_string(cfg)
    expiry = (datetime.now(UTC) + timedelta(minutes=expiry_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    raw = _run_az([
        "az", "storage", "blob", "generate-sas",
        "--container-name", container,
        "--name", blob_name,
        "--connection-string", conn_str,
        "--permissions", "r",
        "--expiry", expiry,
        "-o", "tsv",
    ])
    sas_token = raw.strip().strip('"')
    return f"{_blob_url(container, blob_name)}?{sas_token}"


# Threshold above which we use urlSource (DI fetches from blob) instead
# of sending bytes in the POST body. Avoids server disconnects on large uploads.
_URL_SOURCE_THRESHOLD = 30 * 1024 * 1024  # 30 MB


def analyze_di(cfg: dict, pdf_bytes: bytes, timeout_s: int = 900,
               pdf_name: str = "") -> dict:
    ep = cfg["documentIntelligence"]["endpoint"].rstrip("/")
    api_ver = cfg["documentIntelligence"].get("apiVersion", "2024-11-30")
    url = (
        f"{ep}/documentintelligence/documentModels/prebuilt-layout:analyze"
        f"?api-version={api_ver}&outputContentFormat=markdown"
    )
    api_key = _get_di_key(cfg)
    use_url_source = len(pdf_bytes) > _URL_SOURCE_THRESHOLD and pdf_name

    ssl_verify: str | bool = os.environ.get("SSL_CERT_FILE") or True
    max_submit_retries = 3

    with httpx.Client(
        timeout=httpx.Timeout(connect=30.0, write=600.0, read=300.0, pool=30.0),
        verify=ssl_verify,
    ) as client:
        # Submit with retry
        op_loc = None
        for attempt in range(max_submit_retries):
            try:
                if use_url_source:
                    # Large PDF: give DI a SAS URL so it fetches server-to-server
                    sas_url = _generate_blob_sas(cfg, pdf_name, expiry_minutes=120)
                    headers = {
                        "Ocp-Apim-Subscription-Key": api_key,
                        "Content-Type": "application/json",
                    }
                    submit = client.post(url, headers=headers,
                                         json={"urlSource": sas_url})
                    print(f"      submitted via urlSource ({len(pdf_bytes) / 1024 / 1024:.0f} MB)", flush=True)
                else:
                    headers = {
                        "Ocp-Apim-Subscription-Key": api_key,
                        "Content-Type": "application/pdf",
                    }
                    submit = client.post(url, headers=headers, content=pdf_bytes)

                if submit.status_code not in (200, 202):
                    raise RuntimeError(f"DI submit: {submit.status_code} {submit.text[:300]}")
                op_loc = submit.headers.get("operation-location")
                if not op_loc:
                    raise RuntimeError("DI submit missing operation-location header")
                break  # success
            except (httpx.RemoteProtocolError, httpx.WriteTimeout, httpx.ConnectError) as exc:
                if attempt < max_submit_retries - 1:
                    wait = 10 * (attempt + 1)
                    print(f"      submit failed ({type(exc).__name__}), retrying in {wait}s...", flush=True)
                    time.sleep(wait)
                    continue
                raise

        if not op_loc:
            raise RuntimeError("DI submit failed after all retries")

        deadline = time.time() + timeout_s
        start = time.time()
        backoff = 3.0
        consecutive_poll_errors = 0
        while time.time() < deadline:
            poll_h = {"Ocp-Apim-Subscription-Key": api_key}
            try:
                poll = client.get(op_loc, headers=poll_h)
                body = poll.json()
                status = body.get("status")
            except (httpx.TimeoutException, httpx.ConnectError,
                    httpx.RemoteProtocolError, json.JSONDecodeError) as exc:
                # Transient poll failure. Back off and try again; don't
                # kill an hours-long DI run over one blip.
                consecutive_poll_errors += 1
                if consecutive_poll_errors >= 10:
                    raise RuntimeError(f"DI poll failed 10x in a row: {exc}") from exc
                time.sleep(min(backoff * consecutive_poll_errors, 30.0))
                continue
            consecutive_poll_errors = 0
            if status == "succeeded":
                return body.get("analyzeResult", {})
            if status == "failed":
                raise RuntimeError(f"DI failed: {body}")
            if status not in ("running", "notStarted", None):
                # Unknown status; treat as transient and keep polling.
                print(f"      unexpected DI status '{status}', continuing to poll", flush=True)
            elapsed = int(time.time() - start)
            print(f"      polling... {elapsed}s ({status})", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 1.3, 15.0)
        raise TimeoutError(f"DI timed out after {timeout_s}s")


# -- Triage helpers --

def _passes_triage(polygon: list[float], image_b64: str | None = None) -> tuple[bool, str]:
    """Return (passes, reason). Checks dimensions + aspect ratio + crop size."""
    xs = polygon[0::2]
    ys = polygon[1::2]
    w_in = max(xs) - min(xs)
    h_in = max(ys) - min(ys)

    if w_in < MIN_WIDTH_IN or h_in < MIN_HEIGHT_IN:
        return False, f"tiny ({w_in:.1f}x{h_in:.1f} in)"

    if w_in > 0 and h_in > 0:
        ratio = max(w_in / h_in, h_in / w_in)
        if ratio > MAX_ASPECT_RATIO:
            return False, f"extreme aspect ratio ({ratio:.1f}:1)"

    if image_b64:
        try:
            raw_png = base64.b64decode(image_b64)
            if len(raw_png) < MIN_CROP_BYTES:
                return False, f"small crop ({len(raw_png)} bytes)"
        except Exception:
            pass

    return True, ""


# -- Phase A: DI analysis + cropping --

def _pdf_has_any_crops(cfg: dict, pdf_name: str) -> bool:
    """Cheap probe: does the cache already hold at least one crop blob
    for this PDF? Used to distinguish a fully-done PDF from one whose
    DI cache was written but whose crop phase crashed (e.g. previous
    fitz import failure). A single LIST call covers arbitrary PDF names."""
    try:
        _init_storage(cfg)
        container = cfg["storage"]["pdfContainerName"]
        conn_str = _get_connection_string(cfg)
        raw = _run_az([
            "az", "storage", "blob", "list",
            "--container-name", container,
            "--connection-string", conn_str,
            "--prefix", f"_dicache/{pdf_name}.crop.",
            "--num-results", "1",
            "--query", "[].name",
            "-o", "json",
        ])
        return len(json.loads(raw)) > 0
    except Exception:
        return False


def _is_pdf_done(cfg: dict, pdf_name: str) -> bool:
    """Strict 'fully done' check used by --incremental.

    A PDF is considered done only if output.json exists AND either
      (a) it reports at least one enriched figure (success), OR
      (b) the underlying DI cache confirms the PDF has zero figures
          (so zero enriched figures is legitimate, not a partial state).

    This defends against the case where an earlier run wrote an empty
    output.json because the crop phase had crashed -- without this check
    --incremental would incorrectly skip such PDFs forever.
    """
    output_name = f"_dicache/{pdf_name}.output.json"
    if not blob_exists(cfg, output_name):
        return False

    try:
        out = json.loads(fetch_blob(cfg, output_name))
    except Exception:
        return False  # unreadable output.json -> re-run

    # PRIMARY signal: processing_status. phase_output sets this to "ok"
    # (or "di_missed_images" / "partial_vision") only AFTER the output
    # was assembled cleanly. If it's any of those, preanalyze completed
    # for this PDF regardless of whether DI's figure count is non-zero.
    # This is what catches the legit "DI saw figures, triage rejected
    # them all" case (decorative borders, table edges, page-number
    # graphics, etc.) — common for PDFs that are tables+text only.
    status = out.get("processing_status") or ""
    if status in ("ok", "di_missed_images", "partial_vision"):
        return True

    # Fallback: older output.json files predating processing_status.
    enriched_count = len(out.get("enriched_figures") or [])
    if enriched_count > 0:
        return True

    # Zero enriched figures and no processing_status. Verify against DI
    # cache: if DI also reported zero figures, output.json is legitimately
    # done. If DI had figures, this is a partial-state carryover.
    di_name = f"_dicache/{pdf_name}.di.json"
    if not blob_exists(cfg, di_name):
        return True  # DI cache gone; trust output.json
    try:
        di = json.loads(fetch_blob(cfg, di_name))
    except Exception:
        return True  # can't verify; don't re-do good work
    di_figure_count = len(di.get("figures") or [])
    return di_figure_count == 0


def phase_di(cfg: dict, pdf_name: str, force: bool) -> str:
    """Run DI analysis and crop figures. Cache results to blob.

    Resumable: if DI cache exists but zero crops do, re-runs only the
    crop+upload phase using the cached DI result (no extra DI call).

    Non-PDF inputs (.docx/.pptx/.xlsx) skip the cropping step entirely:
    DI extracts text + tables, but PyMuPDF cannot render figures from
    those formats. The output.json carries empty enriched_figures and
    full enriched_tables, so the indexer still produces text and table
    records for them.
    """
    cache_name = "_dicache/" + pdf_name + ".di.json"
    is_pdf = _is_pdf(pdf_name)

    if not force and blob_exists(cfg, cache_name):
        if _pdf_has_any_crops(cfg, pdf_name):
            return f"  skip-di  {pdf_name} (DI cached + crops present)"
        if not is_pdf:
            # Non-PDF: no crops expected, ever. Treat as done.
            return f"  skip-di  {pdf_name} (non-PDF, DI cached, no crops needed)"
        # DI cache exists but crops don't -- previous run crashed between
        # DI upload and crop upload. Resume without re-running DI.
        print(f"  resume-crops  {pdf_name} (DI cached, crops missing)", flush=True)
        try:
            di_cache_bytes = fetch_blob(cfg, cache_name)
            result = json.loads(di_cache_bytes)
            pdf_bytes = fetch_blob(cfg, pdf_name)
            elapsed = 0.0
        except Exception as exc:
            import traceback
            traceback.print_exc()
            return f"  FAIL-di  {pdf_name}: resume-crops fetch failed: {type(exc).__name__}: {exc}"
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "function_app"))
        from shared.pdf_crop import crop_figure_png_b64
        return _do_crops(cfg, pdf_name, pdf_bytes, result, crop_figure_png_b64, elapsed)

    try:
        original_bytes = fetch_blob(cfg, pdf_name)
        size_mb = len(original_bytes) / (1024 * 1024)

        # If the file isn't a PDF, try to convert it via LibreOffice so
        # the rest of the pipeline (DI submission, PyMuPDF cropping,
        # vision) works uniformly. If LibreOffice isn't available, fall
        # back to PDF-only processing (DI on the native file, no figure
        # crops).
        pdf_bytes = original_bytes
        converted = False
        if not is_pdf:
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                from convert import (
                    ConversionError,
                    ConverterNotAvailable,
                    convert_to_pdf,
                )
                t_conv = time.time()
                pdf_bytes = convert_to_pdf(pdf_name, original_bytes)
                conv_elapsed = time.time() - t_conv
                converted = True
                conv_mb = len(pdf_bytes) / (1024 * 1024)
                print(
                    f"  converted {pdf_name} -> PDF ({conv_elapsed:.0f}s, "
                    f"{size_mb:.1f}MB -> {conv_mb:.1f}MB)",
                    flush=True,
                )
            except ConverterNotAvailable as exc:
                print(
                    f"  warn: skipping conversion for {pdf_name} -- "
                    f"LibreOffice not installed. Indexing text + tables only. "
                    f"({exc})",
                    flush=True,
                )
                # pdf_bytes still points at the original (DI handles natively)
            except ConversionError as exc:
                # Non-fatal: fall back to native DI on the original file.
                # We still get text + tables; just no figure crops.
                print(
                    f"  warn: conversion failed for {pdf_name} -- "
                    f"falling back to text+tables only. ({exc})",
                    flush=True,
                )

        kind = "PDF" if is_pdf else (
            f"converted from {Path(pdf_name).suffix}" if converted
            else f"native non-PDF ({Path(pdf_name).suffix})"
        )
        print(f"  DI analyzing {pdf_name} ({size_mb:.1f} MB, {kind}) ...", flush=True)
        t0 = time.time()

        result = analyze_di(cfg, pdf_bytes, pdf_name=pdf_name)
        elapsed = time.time() - t0
        di_bytes = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        upload_blob(cfg, cache_name, di_bytes)
        di_mb = len(di_bytes) / (1024 * 1024)
        print(f"    DI done ({elapsed:.0f}s, {di_mb:.1f} MB)", flush=True)

        # Cropping needs PyMuPDF, which only reads PDFs. PDFs get cropped
        # natively. Non-PDFs that were converted get cropped from the
        # converted bytes. Non-PDFs that we couldn't convert (LibreOffice
        # missing or failed) skip cropping.
        if not is_pdf and not converted:
            return f"  ok-di  {pdf_name} ({elapsed:.0f}s, no conversion -- text + tables only)"

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "function_app"))
        from shared.pdf_crop import crop_figure_png_b64

        return _do_crops(cfg, pdf_name, pdf_bytes, result, crop_figure_png_b64, elapsed)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return f"  FAIL-di  {pdf_name}: {type(exc).__name__}: {exc}"


def _do_crops(cfg: dict, pdf_name: str, pdf_bytes: bytes,
              result: dict, crop_figure_png_b64, elapsed: float) -> str:
    """Shared crop + parallel-upload body used by both the fresh-run and
    resume-crops paths of phase_di. Safe to call when the DI cache already
    exists; only uploads crops that are new (parallel uploader overwrites
    by name, so it is idempotent).

    Fails loud (FAIL-di) on encrypted or corrupt PDFs — silent skip
    would produce an output.json with zero figures and the operator
    would never know why a 200-page manual indexed with no diagrams.
    """
    # Pre-flight check: open the PDF once to surface
    # CorruptPdfError / EncryptedPdfError early. PyMuPDF caches the doc
    # internally per-call so this is essentially free.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "function_app"))
    from shared.pdf_crop import CorruptPdfError, EncryptedPdfError, _open_pdf
    try:
        _open_pdf(pdf_bytes).close()
    except EncryptedPdfError as exc:
        return (f"  FAIL-di  {pdf_name}: PDF is password-protected. "
                f"Remove protection upstream and re-upload. ({exc})")
    except CorruptPdfError as exc:
        return (f"  FAIL-di  {pdf_name}: PDF is corrupted / unreadable. "
                f"Inspect the source file. ({exc})")

    figures = result.get("figures", []) or []
    skip_count = 0
    seen_hashes: dict[str, str] = {}

    pending_uploads: list[tuple[str, bytes]] = []
    for fig_idx, figure in enumerate(figures):
        fig_id = figure.get("id") or f"fig_{fig_idx}"
        page = None
        polygon = None
        for br in figure.get("boundingRegions", []) or []:
            p = br.get("pageNumber")
            poly = br.get("polygon")
            if isinstance(p, int) and poly:
                page = p
                polygon = poly
                break
        if not page or not polygon or len(polygon) < 4:
            print(f"    skip fig {fig_id}: page={page} polygon_len={len(polygon) if polygon else 0}", flush=True)
            continue

        passes, reason = _passes_triage(polygon)
        if not passes:
            skip_count += 1
            continue

        try:
            image_b64, bbox = crop_figure_png_b64(pdf_bytes, page, polygon)

            passes2, reason2 = _passes_triage(polygon, image_b64)
            if not passes2:
                skip_count += 1
                continue

            raw_png = base64.b64decode(image_b64)
            crop_hash = hashlib.sha256(raw_png).hexdigest()
            if crop_hash in seen_hashes:
                skip_count += 1
                continue
            seen_hashes[crop_hash] = fig_id

            crop_obj = {"image_b64": image_b64, "bbox": bbox}
            crop_bytes = json.dumps(crop_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            pending_uploads.append((f"_dicache/{pdf_name}.crop.{fig_id}.json", crop_bytes))
        except Exception as exc:
            print(f"    crop error (fig {fig_id} pg {page}): {exc}", flush=True)
            continue

    crop_count = 0
    if pending_uploads:
        print(f"    uploading {len(pending_uploads)} crops (10 parallel)...", flush=True)

        def _upload_one(item: tuple[str, bytes]) -> bool:
            try:
                upload_blob(cfg, item[0], item[1])
                return True
            except Exception as exc:
                print(f"    upload error ({item[0]}): {exc}", flush=True)
                return False

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(_upload_one, item) for item in pending_uploads]
            for f in as_completed(futures):
                if f.result():
                    crop_count += 1
                if crop_count % 100 == 0 and crop_count > 0:
                    print(f"    {crop_count}/{len(pending_uploads)} uploaded...", flush=True)

    # PyMuPDF supplemental-figure pass: detect raster images that DI
    # didn't pick up and synthesize figure entries so the rest of the
    # pipeline (vision, output assembly, indexer projection) treats them
    # exactly like DI figures. Pre-Sprint-6 this was diagnostic-only —
    # it counted missed images but didn't extract them. Now we crop,
    # upload, and write a supplement file that phase_vision and
    # phase_output read alongside the DI cache.
    di_pages_with_figures: set[int] = set()
    for figure in figures:
        for br in figure.get("boundingRegions", []) or []:
            p = br.get("pageNumber")
            if isinstance(p, int):
                di_pages_with_figures.add(p)

    supplement_figures: list[dict] = []
    supplement_uploads: list[tuple[str, bytes]] = []
    di_missed_pages_detail: list[dict] = []
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            for page_idx in range(doc.page_count):
                page_num = page_idx + 1
                if page_num in di_pages_with_figures:
                    continue
                page = doc.load_page(page_idx)
                raster_images = page.get_images(full=False) or []
                page_substantial = 0
                for img_idx, img_info in enumerate(raster_images):
                    xref = img_info[0]
                    try:
                        rects = page.get_image_rects(xref)
                    except Exception:
                        rects = []
                    for rect in rects:
                        w_in = (rect.x1 - rect.x0) / 72.0
                        h_in = (rect.y1 - rect.y0) / 72.0
                        # Same minimum-size triage as DI figures
                        # (`_passes_triage`): skip tiny logos / bullets.
                        if max(w_in, h_in) < 1.0:
                            continue
                        # Build a DI-shaped polygon (8 numbers in inches)
                        # from the PyMuPDF rect (PDF points). DI emits
                        # polygons in inches at the layout level.
                        x0_in = rect.x0 / 72.0
                        y0_in = rect.y0 / 72.0
                        x1_in = rect.x1 / 72.0
                        y1_in = rect.y1 / 72.0
                        polygon = [
                            x0_in, y0_in,
                            x1_in, y0_in,
                            x1_in, y1_in,
                            x0_in, y1_in,
                        ]
                        passes_geom, _ = _passes_triage(polygon)
                        if not passes_geom:
                            continue

                        # Synthetic figure id encodes page + image index
                        # so it's stable across re-runs (assuming PyMuPDF
                        # enumerates images in the same order — which it
                        # does for non-incremental edits).
                        synthetic_id = f"mupdf_p{page_num}_i{img_idx}"

                        # Render the crop using the same helper DI figures
                        # use, so frontend rendering / hash dedup logic
                        # treats them identically.
                        try:
                            image_b64, bbox = crop_figure_png_b64(
                                pdf_bytes, page_num, polygon
                            )
                        except Exception as crop_exc:
                            print(f"    mupdf crop error (p{page_num} i{img_idx}): {crop_exc}",
                                  flush=True)
                            continue

                        # Re-triage on rendered size (same as DI path).
                        passes_size, _ = _passes_triage(polygon, image_b64)
                        if not passes_size:
                            continue

                        # Hash dedup against existing DI crops AND
                        # already-collected supplemental crops on this
                        # PDF, so the same OEM nameplate appearing on
                        # 5 pages doesn't get cropped 5 times.
                        try:
                            raw_png = base64.b64decode(image_b64)
                        except Exception:
                            continue
                        crop_sha = hashlib.sha256(raw_png).hexdigest()
                        if crop_sha in seen_hashes:
                            continue
                        seen_hashes[crop_sha] = synthetic_id

                        crop_obj = {"image_b64": image_b64, "bbox": bbox}
                        crop_blob = f"_dicache/{pdf_name}.crop.{synthetic_id}.json"
                        supplement_uploads.append((
                            crop_blob,
                            json.dumps(crop_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                        ))

                        # DI-shaped figure dict for the supplement file —
                        # phase_vision and phase_output concat these into
                        # `result["figures"]` and treat them identically
                        # to native DI figures.
                        supplement_figures.append({
                            "id": synthetic_id,
                            "boundingRegions": [{
                                "pageNumber": page_num,
                                "polygon": polygon,
                            }],
                            "_source": "pymupdf_supplement",
                        })
                        page_substantial += 1
                if page_substantial > 0:
                    di_missed_pages_detail.append({
                        "page": page_num,
                        "image_count": page_substantial,
                    })
        finally:
            doc.close()
    except Exception as exc:
        # Non-fatal: a failure here means we miss synthetic figures for
        # this PDF, but the build proceeds with whatever DI detected.
        print(f"    mupdf supplement scan failed for {pdf_name}: {exc}", flush=True)

    supplement_count = 0
    if supplement_uploads:
        print(
            f"    uploading {len(supplement_uploads)} supplemental crops...",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = [
                pool.submit(lambda item: upload_blob(cfg, item[0], item[1]) or True, item)
                for item in supplement_uploads
            ]
            for f in as_completed(futs):
                try:
                    if f.result():
                        supplement_count += 1
                except Exception as exc:
                    print(f"    supplement upload error: {exc}", flush=True)

    # Always write the supplement file (even when empty) so phase_vision
    # and phase_output have a deterministic source of truth — empty
    # supplement means "nothing to add to DI's figures[]".
    try:
        sup_blob = f"_dicache/{pdf_name}.figures_supplement.json"
        sup_payload = {
            "figures": supplement_figures,
            "missed_pages": di_missed_pages_detail[:50],
            "missed_image_pages": len(di_missed_pages_detail),
            "missed_image_count": sum(d["image_count"] for d in di_missed_pages_detail),
        }
        upload_blob(
            cfg, sup_blob,
            json.dumps(sup_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
    except Exception as exc:
        print(f"    supplement write failed for {pdf_name}: {exc}", flush=True)

    # Back-compat: keep writing the .di_warnings.json file so older
    # consumers keep working. New consumers should prefer the supplement.
    if di_missed_pages_detail:
        try:
            warn_blob = f"_dicache/{pdf_name}.di_warnings.json"
            warn_payload = {
                "di_missed_image_pages": len(di_missed_pages_detail),
                "di_missed_image_count": sum(d["image_count"] for d in di_missed_pages_detail),
                "pages": di_missed_pages_detail[:50],
            }
            upload_blob(
                cfg, warn_blob,
                json.dumps(warn_payload, separators=(",", ":")).encode("utf-8"),
            )
        except Exception as exc:
            print(f"    di-missed-image write failed for {pdf_name}: {exc}", flush=True)

    sup_msg = (
        f", auto-extracted {supplement_count} pymupdf figures from "
        f"{len(di_missed_pages_detail)} pages"
    ) if supplement_count else ""

    return f"  ok-di  {pdf_name} ({elapsed:.0f}s, {crop_count} crops, {skip_count} skipped{sup_msg})"


def _load_figures_supplement(cfg: dict, pdf_name: str) -> list[dict]:
    """Load the PyMuPDF figures supplement file written by _do_crops, if
    present. Returns a list of synthetic figure dicts in DI's `figures[]`
    shape so callers can `figures + supplement_figures` them and treat
    them uniformly. Empty list when no supplement exists or it can't
    be parsed.

    Used by phase_vision (so synthetic figures get vision analysis) and
    phase_output (so synthetic figures appear in enriched_figures and
    therefore in the index)."""
    sup_blob = f"_dicache/{pdf_name}.figures_supplement.json"
    try:
        if not blob_exists(cfg, sup_blob):
            return []
        raw = fetch_blob(cfg, sup_blob)
        data = json.loads(raw)
        figs = data.get("figures") or []
        if not isinstance(figs, list):
            return []
        return [f for f in figs if isinstance(f, dict)]
    except Exception as exc:
        print(f"    supplement load failed for {pdf_name}: {exc}", flush=True)
        return []


# -- Phase B: Vision analysis (parallel within each PDF) --

_VISION_MAX_ATTEMPTS = 3


def _vision_one_figure(cfg: dict, pdf_name: str, fig_data: dict, force: bool) -> dict | None:
    """Process a single figure's vision call. Returns fig_data with vision fields, or None.

    Cache semantics:
      - Successful result: cached as vision JSON. Skipped on re-run.
      - Transient error (JSON parse, timeout, etc.): cached as
        {"_error": "...", "_attempts": N}. Retried until N >= _VISION_MAX_ATTEMPTS.
      - Permanent error (content filter): cached with _attempts forced to max so
        we never retry.
    """
    fig_id = fig_data["figure_id"]
    vision_blob = f"_dicache/{pdf_name}.vision.{fig_id}.json"

    prior_attempts = 0
    if not force:
        try:
            if blob_exists(cfg, vision_blob):
                vb = fetch_blob(cfg, vision_blob)
                cached = json.loads(vb)
                if not isinstance(cached, dict) or "_error" not in cached:
                    return cached
                prior_attempts = int(cached.get("_attempts", 0))
                if prior_attempts >= _VISION_MAX_ATTEMPTS:
                    return {}  # give up, treat as "no useful result"
        except Exception:
            pass

    crop_blob = f"_dicache/{pdf_name}.crop.{fig_id}.json"
    try:
        crop_bytes = fetch_blob(cfg, crop_blob)
        crop_data = json.loads(crop_bytes)
        image_b64 = crop_data.get("image_b64", "")
    except Exception:
        return None

    if not image_b64:
        return None

    try:
        user_text = _build_vision_user_text(fig_data)
        vision_result = _call_vision_api(cfg, image_b64, user_text)
        # Defensive accuracy gate: a vision response with a useful category
        # but a description shorter than 30 chars is almost always a
        # truncation or hallucination. Retry once with the same prompt;
        # if the second attempt is also degenerate, accept it as-is so
        # we don't loop forever.
        vision_result = _validate_and_retry_if_degenerate(
            cfg, image_b64, user_text, vision_result, fig_id,
        )
    except Exception as exc:
        # The vision call itself failed (API error, JSON parse, etc.).
        # Record the error so we can retry sparingly and stop retrying
        # permanent failures like content-filter blocks.
        msg = str(exc)
        is_permanent = "content_filter" in msg or "ResponsibleAIPolicy" in msg
        attempts = _VISION_MAX_ATTEMPTS if is_permanent else prior_attempts + 1
        err_record = {"_error": msg[:500], "_attempts": attempts}
        try:
            err_bytes = json.dumps(err_record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            upload_blob(cfg, vision_blob, err_bytes)
        except Exception:
            pass  # if we can't even save the error, next run will still retry
        tag = "permanent" if is_permanent else f"attempt {attempts}/{_VISION_MAX_ATTEMPTS}"
        print(f"    vision error ({fig_id}, {tag}): {exc}", flush=True)
        return {}

    # Vision call succeeded. Cache is best-effort -- if the cache upload fails,
    # log it but still return the result so we don't waste the tokens we paid for.
    try:
        vr_bytes = json.dumps(vision_result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        upload_blob(cfg, vision_blob, vr_bytes)
    except Exception as exc:
        print(f"    cache-save failed ({fig_id}): {exc}  (result still used)", flush=True)
    return vision_result


def phase_vision(cfg: dict, pdf_name: str, force: bool, vision_parallel: int = 20) -> str:
    """Run vision API on all cropped figures with intra-PDF parallelism."""
    cache_name = "_dicache/" + pdf_name + ".di.json"
    output_name = "_dicache/" + pdf_name + ".output.json"

    # Fast path: if the PDF is already fully assembled, don't iterate 2000+
    # figures just to HEAD-check each cached vision blob.
    if not force and blob_exists(cfg, output_name):
        return f"  skip-vision  {pdf_name} (output already cached)"

    if not blob_exists(cfg, cache_name):
        return f"  skip-vision  {pdf_name} (no DI cache -- run --phase di first)"

    try:
        di_bytes = fetch_blob(cfg, cache_name)
        result = json.loads(di_bytes)
        if "analyzeResult" in result:
            result = result["analyzeResult"]

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "function_app"))
        from shared.ids import parent_id_for
        from shared.sections import build_section_index, extract_surrounding_text, find_section_for_page

        sections_index = build_section_index(result)
        account = _account_name(cfg)
        container = cfg["storage"]["pdfContainerName"]
        source_path = f"https://{account}.blob.{_storage_endpoint_suffix}/{container}/{pdf_name}"
        source_file = pdf_name
        parent_id = parent_id_for(source_path, source_file)

        # Concatenate DI figures with PyMuPDF-supplemental figures so
        # vision is called on raster schematics DI missed.
        figures = (result.get("figures", []) or []) + _load_figures_supplement(cfg, pdf_name)
        fig_tasks: list[dict] = []

        for fig_idx, figure in enumerate(figures):
            fig_id = figure.get("id") or f"fig_{fig_idx}"
            page = None
            polygon = None
            for br in figure.get("boundingRegions", []) or []:
                p = br.get("pageNumber")
                poly = br.get("polygon")
                if isinstance(p, int) and poly:
                    page = p
                    polygon = poly
                    break
            if not page or not polygon or len(polygon) < 4:
                continue

            passes, _ = _passes_triage(polygon)
            if not passes:
                continue

            crop_blob = f"_dicache/{pdf_name}.crop.{fig_id}.json"
            if not blob_exists(cfg, crop_blob):
                continue

            cap = figure.get("caption") or {}
            caption = (cap.get("content") or "").strip()

            section = find_section_for_page(sections_index, page)
            h1 = section["header_1"] if section else ""
            h2 = section["header_2"] if section else ""
            h3 = section["header_3"] if section else ""
            # 400 chars before + 400 after the caption matches what
            # process_document.py uses in the live-DI path. Wider window
            # captures multi-paragraph procedural context that grounds
            # the figure ("after de-energizing per Section 4.1, locate
            # the relay shown in Figure 18.117 and..." needs the full
            # 400 chars on each side).
            surrounding = extract_surrounding_text(section["content"], caption, chars=400) if section else ""

            fig_tasks.append({
                "figure_id": fig_id,
                "page_number": page,
                "caption": caption,
                "header_1": h1, "header_2": h2, "header_3": h3,
                "surrounding_context": surrounding,
                "source_file": source_file,
                "source_path": source_path,
                "parent_id": parent_id,
            })

        if not fig_tasks:
            return f"  skip-vision  {pdf_name} (no figures to process)"

        # Pre-cache the AOAI key before spawning threads so 20 threads
        # don't race to call az CLI simultaneously.
        try:
            _get_aoai_key(cfg)
        except Exception as exc:
            return f"  FAIL-vision  {pdf_name}: could not fetch AOAI key: {exc}"

        print(f"  vision {pdf_name}: {len(fig_tasks)} figures, {vision_parallel} parallel...", flush=True)
        t0 = time.time()
        done = 0
        useful = 0

        with ThreadPoolExecutor(max_workers=vision_parallel) as pool:
            futures = {
                pool.submit(_vision_one_figure, cfg, pdf_name, fd, force): fd
                for fd in fig_tasks
            }
            for f in as_completed(futures):
                done += 1
                try:
                    vr = f.result()
                except Exception as exc:
                    # A figure thread crashed with an uncaught exception.
                    # Record and move on so one figure doesn't nuke the whole PDF.
                    fd = futures[f]
                    print(f"    vision crash ({fd.get('figure_id', '?')}): "
                          f"{type(exc).__name__}: {exc}", flush=True)
                    vr = None
                if vr and vr.get("is_useful"):
                    useful += 1
                if done % 50 == 0:
                    print(f"    {pdf_name}: {done}/{len(fig_tasks)} vision calls done...", flush=True)

        elapsed = time.time() - t0
        return f"  ok-vision  {pdf_name} ({done} calls, {useful} useful, {elapsed:.0f}s)"
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return f"  FAIL-vision  {pdf_name}: {type(exc).__name__}: {exc}"


# -- Phase C: Assemble output.json --

def phase_output(cfg: dict, pdf_name: str, force: bool) -> str:
    """Assemble the final output.json from cached DI + vision + crop results."""
    output_name = "_dicache/" + pdf_name + ".output.json"
    cache_name = "_dicache/" + pdf_name + ".di.json"

    if not force and blob_exists(cfg, output_name):
        return f"  skip-output  {pdf_name} (output cached)"

    if not blob_exists(cfg, cache_name):
        return f"  skip-output  {pdf_name} (no DI cache)"

    try:
        di_bytes = fetch_blob(cfg, cache_name)
        result = json.loads(di_bytes)
        if "analyzeResult" in result:
            result = result["analyzeResult"]

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "function_app"))
        from shared.ids import SKILL_VERSION, parent_id_for
        from shared.page_label import cover_metadata_from_analyze
        from shared.sections import build_section_index, extract_surrounding_text, find_section_for_page
        from shared.tables import extract_table_records

        sections_index = build_section_index(result)

        # Persist section_index as a sidecar blob so the function-app
        # skill chain at indexer time can load it in ~1 sec instead of
        # rebuilding from the 23 MB DI cache (which takes 30 sec - 3 min
        # for huge PDFs and was blowing past Azure's 230s WebApi skill
        # timeout). This is the only way to keep extract_page_label
        # within budget for documents with 2700+ sections.
        try:
            sections_blob = f"_dicache/{pdf_name}.sections.json"
            sections_bytes = json.dumps(
                sections_index, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            upload_blob(cfg, sections_blob, sections_bytes)
            print(f"  ok-sections  {pdf_name} ({len(sections_index)} sections, "
                  f"{len(sections_bytes) // 1024} KiB)", flush=True)
        except Exception as exc:
            print(f"  warn: sections sidecar upload failed for {pdf_name}: "
                  f"{type(exc).__name__}: {exc}", flush=True)

        account = _account_name(cfg)
        container = cfg["storage"]["pdfContainerName"]
        source_path = f"https://{account}.blob.{_storage_endpoint_suffix}/{container}/{pdf_name}"
        source_file = pdf_name
        parent_id = parent_id_for(source_path, source_file)

        # Total physical page count of the source PDF, derived from DI's
        # `pages[]` array. Stamped onto every enriched_figure and
        # enriched_table dict so the analyze-diagram + shape-table skills
        # can read `/document/enriched_figures/*/pdf_total_pages` etc.
        # without the indexer logging "Missing or empty value" warnings
        # (one per item, hundreds in aggregate). Mirrors the field
        # process_document.py emits in the live-DI fallback path.
        pages_array = result.get("pages") or []
        pdf_total_pages = len(pages_array) if pages_array else None

        # Cover metadata extracted ONCE here from the in-memory DI result
        # and propagated through every enriched_figure / enriched_table +
        # top-level output. Without this, the function-app's process-
        # document skill at indexer time would have to backfill these
        # fields by re-fetching and re-parsing the DI cache -- which on
        # huge PDFs (20+ MB cache) blew past the 30-min function timeout
        # and cascade-killed every in-flight skill invocation. Computing
        # here is essentially free (the analyze result is already loaded).
        cover_meta = cover_metadata_from_analyze(result)
        document_revision = cover_meta["document_revision"]
        effective_date = cover_meta["effective_date"]
        document_number = cover_meta["document_number"]

        enriched_figures = []
        # Counters for vision-coverage health check (emitted at end of phase).
        # If vision was killed mid-run, many figures will have no vision
        # sidecar -- silently emitting them as has_diagram=False is a data-
        # loss bug. Track and FAIL the phase if coverage is below threshold.
        vision_total = 0       # figures that reached the vision-blob lookup
        vision_present = 0     # figures whose vision sidecar existed and parsed
        vision_missing = 0     # figures with NO vision sidecar at all
        vision_errored = 0     # figures whose sidecar had _error marker
        # Concat with PyMuPDF supplement so synthetic figures appear in
        # output.json's enriched_figures and reach the index.
        figures = (result.get("figures", []) or []) + _load_figures_supplement(cfg, pdf_name)
        for fig_idx, figure in enumerate(figures):
            fig_id = figure.get("id") or f"fig_{fig_idx}"
            page = None
            polygon = None
            for br in figure.get("boundingRegions", []) or []:
                p = br.get("pageNumber")
                poly = br.get("polygon")
                if isinstance(p, int) and poly:
                    page = p
                    polygon = poly
                    break
            if not page or not polygon or len(polygon) < 4:
                continue

            passes, _ = _passes_triage(polygon)
            if not passes:
                continue

            crop_blob = f"_dicache/{pdf_name}.crop.{fig_id}.json"
            if not blob_exists(cfg, crop_blob):
                # Mirror the warning that the function-app side emits in the
                # same case (process_document.py): silently dropping a
                # figure here would leave a gap between DI's expected
                # figure list and what gets indexed, and the operator
                # would have no clue why.
                print(f"    warn: crop missing for {pdf_name} fig {fig_id} -- skipping", flush=True)
                continue

            cap = figure.get("caption") or {}
            caption = (cap.get("content") or "").strip()

            # Load crop bbox (but NOT image_b64 -- keep output.json small).
            # Narrow exception scope so transient storage failures don't
            # silently emit bbox-less figures (UI cannot draw highlight
            # rectangle). A genuine JSON-malformed cache file is a real
            # data problem the operator should see -- treat the bbox as
            # empty and log; let httpx errors propagate so the run fails
            # and the operator re-runs that PDF rather than producing a
            # broken output.json.
            try:
                crop_data = json.loads(fetch_blob(cfg, crop_blob))
                bbox = crop_data.get("bbox", {})
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                print(f"    warn: crop bbox parse failed for {pdf_name} fig {fig_id}: {exc}",
                      flush=True)
                bbox = {}

            section = find_section_for_page(sections_index, page)
            h1 = section["header_1"] if section else ""
            h2 = section["header_2"] if section else ""
            h3 = section["header_3"] if section else ""
            # 400 chars before + 400 after the caption matches what
            # process_document.py uses in the live-DI path. Wider window
            # captures multi-paragraph procedural context that grounds
            # the figure ("after de-energizing per Section 4.1, locate
            # the relay shown in Figure 18.117 and..." needs the full
            # 400 chars on each side).
            surrounding = extract_surrounding_text(section["content"], caption, chars=400) if section else ""

            fig_data = {
                "figure_id": fig_id,
                "page_number": page,
                "caption": caption,
                "image_b64": "",
                "bbox": bbox,
                "header_1": h1, "header_2": h2, "header_3": h3,
                "surrounding_context": surrounding,
                "source_file": source_file,
                "source_path": source_path,
                "parent_id": parent_id,
                # Per-item pdf_total_pages so the analyze-diagram skill's
                # input mapping (`/document/enriched_figures/*/pdf_total_pages`)
                # resolves cleanly. Without it, the indexer logs a warning
                # per figure -- ~167 warnings on a typical MANGOS manual.
                "pdf_total_pages": pdf_total_pages,
                # Cover metadata propagated so analyze-diagram can stamp
                # document_revision / effective_date / document_number on
                # the emitted diagram record. Without these on input, the
                # skill's fallback was the slow path that timed out.
                "document_revision": document_revision,
                "effective_date": effective_date,
                "document_number": document_number,
            }

            vision_blob = f"_dicache/{pdf_name}.vision.{fig_id}.json"
            vision_result: dict = {}
            # Narrow exception scope. The previous bare `except Exception: pass`
            # silently demoted figures to has_diagram=False whenever the
            # vision blob fetch failed transiently -- the figure would
            # then be cached as a "skipped" record in output.json and the
            # operator had no signal that vision had actually succeeded
            # but the blob read failed. Now: JSON-malformed blobs are
            # logged-and-skipped (real data corruption the operator
            # should know about); transient httpx/runtime errors from
            # fetch_blob propagate up so the run aborts and can be
            # re-tried, rather than silently producing a broken output.json.
            vision_total += 1
            try:
                if blob_exists(cfg, vision_blob):
                    cached = json.loads(fetch_blob(cfg, vision_blob))
                    # Error-cached records (from _vision_one_figure retry
                    # logic) have an "_error" key -- treat as no useful result.
                    if isinstance(cached, dict) and "_error" not in cached:
                        vision_result = cached
                        vision_present += 1
                    else:
                        vision_errored += 1
                else:
                    vision_missing += 1
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                print(f"    warn: vision-blob parse failed for {pdf_name} fig {fig_id}: {exc}",
                      flush=True)
                vision_errored += 1

            v_category = (vision_result.get("category") or "unknown").strip().lower()
            v_description = (vision_result.get("description") or "").strip()
            v_figure_ref = (vision_result.get("figure_ref") or "").strip()
            v_ocr_text = (vision_result.get("ocr_text") or "").strip()
            v_is_useful = bool(vision_result.get("is_useful"))
            v_has_diagram = v_is_useful and v_category in USEFUL_CATEGORIES and bool(v_description)

            if not v_figure_ref:
                m = FIGURE_REF_RE.search(caption or surrounding or "")
                if m:
                    v_figure_ref = f"{m.group(1).title()} {m.group(2)}"

            full_description = v_description
            if v_ocr_text:
                full_description = f"{v_description}\nLabels: {v_ocr_text}"

            fig_data["vision_description"] = full_description
            fig_data["vision_category"] = v_category
            fig_data["vision_figure_ref"] = v_figure_ref
            fig_data["vision_has_diagram"] = v_has_diagram
            fig_data["vision_is_useful"] = v_is_useful

            enriched_figures.append(fig_data)

        enriched_tables = []
        from shared.sections import find_section_for_page_range
        for tbl in extract_table_records(result):
            # Page-range section lookup: tables that cross section
            # boundaries inherit the section containing the bulk of
            # their content, not just page_start's section.
            section = find_section_for_page_range(
                sections_index, tbl["page_start"], tbl.get("page_end")
            )
            h1 = section["header_1"] if section else ""
            h2 = section["header_2"] if section else ""
            h3 = section["header_3"] if section else ""
            # Stamp pdf_total_pages on each row record too. The
            # embed-table-row-chunks skill iterates
            # `/document/enriched_tables/*/tbl_table_rows/*` and any
            # downstream skill reading row-level pdf_total_pages would
            # otherwise see null per row.
            row_records = tbl.get("table_rows", []) or []
            for row in row_records:
                if isinstance(row, dict) and "pdf_total_pages" not in row:
                    row["pdf_total_pages"] = pdf_total_pages
            enriched_tables.append({
                "table_index": tbl["index"],
                "table_rows": row_records,
                "page_start": tbl["page_start"],
                "page_end": tbl["page_end"],
                "markdown": tbl["markdown"],
                "row_count": tbl["row_count"],
                "col_count": tbl["col_count"],
                "caption": tbl["caption"],
                "bboxes": tbl.get("bboxes", []),
                "header_1": h1, "header_2": h2, "header_3": h3,
                "source_file": source_file,
                "source_path": source_path,
                "parent_id": parent_id,
                # Per-item pdf_total_pages -- same rationale as the
                # enriched_figures item above. Without this, every
                # shape-table skill invocation logs a warning.
                "pdf_total_pages": pdf_total_pages,
                # Cover metadata propagated to shape-table input -- same
                # rationale as enriched_figures above.
                "document_revision": document_revision,
                "effective_date": effective_date,
                "document_number": document_number,
            })

        # Read DI-missed-image warnings (written during _do_crops). When
        # present, surface counts in the output so process_document.py
        # can stamp `processing_status="di_missed_images"` for the
        # operator dashboard.
        di_warnings = {}
        warn_blob = f"_dicache/{pdf_name}.di_warnings.json"
        try:
            if blob_exists(cfg, warn_blob):
                di_warnings = json.loads(fetch_blob(cfg, warn_blob))
        except Exception:
            pass

        output_status = "ok"
        if di_warnings.get("di_missed_image_pages"):
            output_status = "di_missed_images"

        # Vision-coverage health check. If MORE than 10% of figures had
        # no vision sidecar at all (not just errored, but never written),
        # phase_vision was almost certainly killed mid-run. Refuse to
        # silently bake those figures into the index as has_diagram=False
        # -- the operator needs to re-run --phase vision before --phase
        # output. This stops the "30% of figures silently missing" data-
        # loss pattern that's been hard to debug from the index side.
        if vision_total > 0:
            miss_pct = (vision_missing / vision_total) * 100
            if miss_pct > 10.0:
                msg = (
                    f"vision coverage too low: {vision_missing}/{vision_total} "
                    f"figures ({miss_pct:.1f}%) have NO vision sidecar. "
                    f"Run preanalyze.py --phase vision --force --pdf {pdf_name} "
                    f"before --phase output."
                )
                return f"  FAIL-output  {pdf_name}: VisionCoverage: {msg}"
            if vision_missing > 0 or vision_errored > 0:
                print(f"    warn: vision coverage {vision_present}/{vision_total} "
                      f"(missing={vision_missing}, errored={vision_errored}) for {pdf_name}",
                      flush=True)
                output_status = "partial_vision"

        output = {
            "enriched_figures": enriched_figures,
            "enriched_tables": enriched_tables,
            "pdf_total_pages": pdf_total_pages,
            # Top-level cover_meta so build-doc-summary can read it from
            # /document/document_revision (no per-item enriched_* path).
            "document_revision": document_revision,
            "effective_date": effective_date,
            "document_number": document_number,
            "processing_status": output_status,
            "di_missed_image_pages": di_warnings.get("di_missed_image_pages", 0),
            "di_missed_image_count": di_warnings.get("di_missed_image_count", 0),
            "vision_coverage_total": vision_total,
            "vision_coverage_present": vision_present,
            "vision_coverage_missing": vision_missing,
            "vision_coverage_errored": vision_errored,
            "skill_version": SKILL_VERSION,
        }
        output_bytes = json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        upload_blob(cfg, output_name, output_bytes)

        vision_ok = sum(1 for f in enriched_figures if f.get("vision_has_diagram"))
        return f"  ok-output  {pdf_name} ({len(enriched_figures)} figs, {vision_ok} useful, {len(enriched_tables)} tables)"
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return f"  FAIL-output  {pdf_name}: {type(exc).__name__}: {exc}"


# -- Status: show per-PDF cache state --

def status_report(cfg: dict) -> None:
    """Print a table showing DI / vision / output cache state for every PDF.

    Fast by design: one LIST call for PDFs, one LIST call for cache blobs,
    then pure in-memory string matching. PDFs that initially look PARTIAL
    (output.json exists but no crops) are verified against the DI cache —
    if DI itself reported zero figures (legitimately, e.g. a 1-page form
    with no diagrams), they are reclassified as DONE. This avoids the
    long-standing bug where figureless documents looked stuck.
    """
    print("Listing PDFs...")
    pdfs = list_pdfs(cfg)
    print(f"Found {len(pdfs)} PDFs")

    if not pdfs:
        print("(no PDFs in container)")
        return

    print("Listing cache blobs...")
    cache_blobs = list_cache_blobs(cfg)
    cache_set = set(cache_blobs)
    print(f"Found {len(cache_blobs)} cache blobs\n")

    # Count vision blobs per PDF by prefix match. We cannot distinguish
    # error-cached from success-cached without downloading each blob, so
    # we just count total -- the end-of-run summary in a real run shows
    # the error split.
    vision_count: dict[str, int] = {pdf: 0 for pdf in pdfs}
    for blob in cache_blobs:
        if ".vision." not in blob:
            continue
        stem = blob.replace("_dicache/", "", 1)
        lower = stem.lower()
        idx = lower.find(".pdf")
        if idx == -1:
            continue
        pdf = stem[: idx + 4]
        if pdf in vision_count:
            vision_count[pdf] += 1

    # Count crop blobs per PDF alongside vision blobs so the table flags
    # PDFs with output.json but no crops ("partial" state from a crashed
    # earlier run).
    crop_count: dict[str, int] = {pdf: 0 for pdf in pdfs}
    for blob in cache_blobs:
        if ".crop." not in blob:
            continue
        stem = blob.replace("_dicache/", "", 1)
        lower = stem.lower()
        idx = lower.find(".pdf")
        if idx == -1:
            continue
        pdf = stem[: idx + 4]
        if pdf in crop_count:
            crop_count[pdf] += 1

    name_w = max(len(p) for p in pdfs)
    name_w = max(name_w, 20)
    header = f"{'PDF'.ljust(name_w)}  DI    Output     Crops   Vision   State"
    print(header)
    print("-" * len(header))

    # When a PDF looks PARTIAL (output.json + DI cache present but zero
    # crops), download just its DI cache to check whether DI itself reported
    # zero figures. If it did, the PDF is actually DONE. This adds at most
    # one blob fetch per ambiguous PDF -- negligible for a healthy index.
    di_figure_count_cache: dict[str, int | None] = {}

    def _di_figure_count(pdf_name: str) -> int | None:
        if pdf_name in di_figure_count_cache:
            return di_figure_count_cache[pdf_name]
        di_blob = f"_dicache/{pdf_name}.di.json"
        if di_blob not in cache_set:
            di_figure_count_cache[pdf_name] = None
            return None
        try:
            data = json.loads(fetch_blob(cfg, di_blob))
            # DI cache may be wrapped in {"analyzeResult": {...}} or be the
            # bare analyzeResult.
            inner = data.get("analyzeResult") if isinstance(data, dict) else None
            inner = inner or data
            n = len(inner.get("figures") or []) if isinstance(inner, dict) else 0
        except Exception:
            n = None
        di_figure_count_cache[pdf_name] = n
        return n

    # Read output.json's `processing_status` for PDFs that look ambiguous.
    # If processing_status is "ok" / "di_missed_images" / "partial_vision",
    # phase_output completed cleanly -- accept as done even if crops == 0
    # (means DI's figures were all triaged out, e.g. decorative borders).
    output_status_cache: dict[str, str | None] = {}

    def _output_processing_status(pdf_name: str) -> str | None:
        if pdf_name in output_status_cache:
            return output_status_cache[pdf_name]
        out_blob = f"_dicache/{pdf_name}.output.json"
        if out_blob not in cache_set:
            output_status_cache[pdf_name] = None
            return None
        try:
            out = json.loads(fetch_blob(cfg, out_blob))
            s = out.get("processing_status") if isinstance(out, dict) else None
        except Exception:
            s = None
        output_status_cache[pdf_name] = s
        return s

    total_done_pdfs = 0
    total_partial = 0
    total_legit_zero_figs = 0
    for pdf in sorted(pdfs):
        di_ok = f"_dicache/{pdf}.di.json" in cache_set
        output_ok = f"_dicache/{pdf}.output.json" in cache_set
        crops = crop_count.get(pdf, 0)
        vision = vision_count.get(pdf, 0)

        # Classify state: done, partial (output without crops), or missing.
        # Paths to "done":
        #   (a) output.json + crops present (typical happy path)
        #   (b) output.json with zero crops AND DI itself reported zero
        #       figures (legit no-figure PDF — 1-2 page forms, etc.)
        #   (c) output.json with zero crops, DI reported figures, BUT
        #       output.json's processing_status is "ok"/"di_missed_images"/
        #       "partial_vision" — means DI's "figures" were all decorative
        #       and triage correctly rejected them. Common for table+text
        #       PDFs where DI flags borders/headers as figures.
        if output_ok and crops == 0 and di_ok:
            out_status = _output_processing_status(pdf)
            if out_status in ("ok", "di_missed_images", "partial_vision"):
                # phase_output completed cleanly -- accept as done.
                state = "done"
                total_done_pdfs += 1
                total_legit_zero_figs += 1
            else:
                di_figs = _di_figure_count(pdf)
                if di_figs == 0:
                    state = "done"  # legitimately no figures
                    total_done_pdfs += 1
                    total_legit_zero_figs += 1
                elif di_figs is None:
                    # Couldn't read DI cache -- fall back to old behavior
                    state = "PARTIAL"
                    total_partial += 1
                else:
                    # DI had figures, no crops, processing_status missing
                    # or indicates a real failure = real partial
                    state = "PARTIAL"
                    total_partial += 1
        elif output_ok:
            state = "done"
            total_done_pdfs += 1
        elif di_ok:
            state = "di-only"
        else:
            state = "todo"

        di_tag = "OK  " if di_ok else "--  "
        out_tag = "OK      " if output_ok else "--      "
        c_str = str(crops) if crops else "--"
        v_str = str(vision) if vision else "--"
        print(f"{pdf.ljust(name_w)}  {di_tag}  {out_tag}  {c_str:>5}   {v_str:>5}   {state}")

    remaining = len(pdfs) - total_done_pdfs
    print()
    msg = f"Summary: {total_done_pdfs}/{len(pdfs)} PDFs fully done, {remaining} remaining"
    if total_legit_zero_figs:
        msg += (f"\n         {total_legit_zero_figs} of those have legitimately "
                "zero figures (1-2 page forms, table-only docs, etc.) -- they "
                "are correctly classified as done.")
    if total_partial:
        msg += (f"\n         {total_partial} in PARTIAL state "
                "-- re-run `preanalyze --incremental` to heal.")
    print(msg)


# -- Cleanup: remove orphaned cache --

def cleanup_orphans(cfg: dict) -> None:
    """Delete _dicache/ blobs whose source PDF no longer exists."""
    print("Listing PDFs...")
    pdfs = set(list_pdfs(cfg))
    print(f"Found {len(pdfs)} PDFs")
    print("Listing cache blobs...")
    cache_blobs = list_cache_blobs(cfg)
    print(f"Found {len(cache_blobs)} cache blobs")

    orphaned = []
    for blob_name in cache_blobs:
        # _dicache/manual.pdf.di.json -> manual.pdf  (case-insensitive on .pdf)
        stem = blob_name.replace("_dicache/", "", 1)
        lower = stem.lower()
        idx = lower.find(".pdf")
        pdf_name = stem[: idx + 4] if idx != -1 else None
        if pdf_name and pdf_name not in pdfs:
            orphaned.append(blob_name)

    if not orphaned:
        print("No orphaned cache blobs found.")
        return

    print(f"Deleting {len(orphaned)} orphaned cache blobs...")
    for name in orphaned:
        delete_blob(cfg, name)
    print(f"Deleted {len(orphaned)} orphaned blobs.")


# -- Legacy: full single-pass (all phases combined) --

def process_one_full(cfg: dict, pdf_name: str, force: bool, vision_parallel: int) -> str:
    """Run all three phases for a single PDF. Intermediate phases print their
    own line; the final phase's line is returned and printed by the caller."""
    r1 = phase_di(cfg, pdf_name, force)
    if "FAIL" in r1:
        return r1
    print(r1, flush=True)

    r2 = phase_vision(cfg, pdf_name, force, vision_parallel)
    if "FAIL" in r2:
        return r2
    print(r2, flush=True)

    r3 = phase_output(cfg, pdf_name, force)
    return r3


# -- Main --

def main() -> None:
    ap = argparse.ArgumentParser(description="Pre-analyze PDFs with Document Intelligence")
    ap.add_argument("--config", default="deploy.config.json")
    ap.add_argument("--force", action="store_true", help="Re-analyze even if cache exists")
    ap.add_argument("--concurrency", type=int, default=1, help="Parallel PDFs (default 1)")
    ap.add_argument("--vision-parallel", type=int, default=30,
                    help="Parallel vision calls per PDF (default 30). "
                         "Raise to 50-80 if your AOAI deployment has high TPM. "
                         "Lower to 5-10 if you see persistent 429s.")
    ap.add_argument("--phase", choices=["di", "vision", "output", "all"], default="all",
                    help="Run a specific phase (default: all)")
    ap.add_argument("--incremental", action="store_true",
                    help="Only process PDFs without an output.json cache")
    ap.add_argument("--cleanup", action="store_true",
                    help="Delete orphaned cache blobs for deleted PDFs")
    ap.add_argument("--status", action="store_true",
                    help="Print per-PDF cache state (no work done)")
    ap.add_argument("--no-lock", action="store_true",
                    help="Skip the pipeline lock. Use only when you are CERTAIN "
                         "no other preanalyze/reconcile is running. Required "
                         "with --status / --cleanup since those are read/audit "
                         "operations.")
    ap.add_argument("--pdf", action="append", default=None, metavar="NAME",
                    help="Process only the named PDF(s). Pass multiple times "
                         "to target multiple PDFs (e.g. --pdf a.pdf --pdf b.pdf). "
                         "Use the filename ONLY (no path). Useful for surgical "
                         "recovery of specific PARTIAL-state PDFs without "
                         "re-processing the whole batch.")
    args = ap.parse_args()

    # Bounds check user-facing knobs. AOAI TPM quotas cap useful parallelism
    # well below 100; concurrency above 6 rarely helps and adds thread
    # pressure. Fail fast with a clear message instead of producing weird
    # runtime behavior.
    if not 1 <= args.vision_parallel <= 100:
        raise SystemExit(
            f"--vision-parallel must be between 1 and 100 (got {args.vision_parallel})"
        )
    if not 1 <= args.concurrency <= 10:
        raise SystemExit(
            f"--concurrency must be between 1 and 10 (got {args.concurrency})"
        )

    cfg = load_config(Path(args.config))

    if args.cleanup:
        cleanup_orphans(cfg)
        return

    if args.status:
        status_report(cfg)
        return

    # Acquire pipeline lock for write operations. --status and --cleanup
    # above are read/audit-only and don't need it. Skipped when --no-lock
    # is passed (use only when you've verified nothing else is running).
    lock_id = None
    lock_name = "preanalyze"
    if not args.no_lock:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from pipeline_lock import LockHeldError, acquire_lock, release_lock
            lock_id = acquire_lock(cfg, lock_name)
            print(f"  acquired pipeline lock '{lock_name}' (id={lock_id[:8]}...)", flush=True)
        except LockHeldError as exc:
            raise SystemExit(f"\nABORT: {exc}") from exc
        except Exception as exc:
            # Storage init failed or similar; surface clearly but don't fail
            # silently — operator should know lock isn't protecting them.
            print(f"  warning: could not acquire pipeline lock: {exc}", flush=True)
            print("  proceeding without lock; pass --no-lock to silence this", flush=True)

    try:
        _run_preanalyze_main(cfg, args)
    finally:
        if lock_id is not None:
            try:
                release_lock(cfg, lock_name, lock_id)
                print(f"  released pipeline lock '{lock_name}'", flush=True)
            except Exception as exc:
                print(f"  warning: lock release failed: {exc}", flush=True)


def _run_preanalyze_main(cfg: dict, args) -> None:
    """The original main() body, factored out so the lock can wrap it."""
    print(f"Container: {cfg['storage']['pdfContainerName']}")
    print(f"DI endpoint: {cfg['documentIntelligence']['endpoint']}")
    print(f"Phase: {args.phase}  Force: {args.force}  Concurrency: {args.concurrency}  Vision parallel: {args.vision_parallel}\n")

    print("Listing PDFs...")
    pdfs = list_pdfs(cfg)
    print(f"Found {len(pdfs)} PDFs")

    # --pdf filter: surgical targeting of specific PDFs. Applied BEFORE
    # the incremental check so the user can re-process specific PDFs
    # regardless of cache state. When combined with --force, also clears
    # any stale output.json so the full preanalyze pipeline runs cleanly.
    if args.pdf:
        requested = set(args.pdf)
        missing = requested - set(pdfs)
        if missing:
            print(f"ERROR: requested PDFs not found in container: {sorted(missing)}")
            print(f"Available PDFs (first 10): {sorted(pdfs)[:10]}")
            return
        pdfs = [p for p in pdfs if p in requested]
        print(f"--pdf filter: targeting {len(pdfs)} PDF(s): {pdfs}")
        # With --force, proactively clear stale output.json so phase_vision
        # and phase_output don't short-circuit on the skip-output check.
        # This is the fix for the PARTIAL-state heal that --incremental
        # alone couldn't recover.
        if args.force:
            for p in pdfs:
                stale = f"_dicache/{p}.output.json"
                if blob_exists(cfg, stale):
                    try:
                        delete_blob(cfg, stale)
                        print(f"  cleared stale output.json for {p}")
                    except Exception as exc:
                        print(f"  warning: could not delete stale output for {p}: {exc}")

    if args.incremental:
        before = len(pdfs)
        needs_process: list[str] = []
        stale_count = 0
        for p in pdfs:
            if _is_pdf_done(cfg, p):
                continue
            # Not done. If a stale output.json is in the way, delete it so
            # phase_output regenerates cleanly instead of short-circuiting
            # on the skip-output check.
            stale_output = f"_dicache/{p}.output.json"
            if blob_exists(cfg, stale_output):
                try:
                    delete_blob(cfg, stale_output)
                    stale_count += 1
                    print(f"  cleared stale output.json for {p} (was partial)", flush=True)
                except Exception as exc:
                    print(f"  warning: could not delete stale output for {p}: {exc}", flush=True)
            needs_process.append(p)
        pdfs = needs_process
        msg = f"Incremental: {before - len(pdfs)} already cached, {len(pdfs)} to process"
        if stale_count:
            msg += f" ({stale_count} stale output.json cleared)"
        print(msg)

    if not pdfs:
        print("Nothing to process.")
        return

    print()

    phase_func = {
        "di": lambda cfg, name, force: phase_di(cfg, name, force),
        "vision": lambda cfg, name, force: phase_vision(cfg, name, force, args.vision_parallel),
        "output": lambda cfg, name, force: phase_output(cfg, name, force),
        "all": lambda cfg, name, force: process_one_full(cfg, name, force, args.vision_parallel),
    }[args.phase]

    results: list[str] = []
    if args.concurrency <= 1:
        for name in pdfs:
            try:
                r = phase_func(cfg, name, args.force)
            except Exception as exc:
                r = f"  FAIL-crash  {name}: {type(exc).__name__}: {exc}"
            results.append(r)
            print(r)
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(phase_func, cfg, name, args.force): name
                for name in pdfs
            }
            for f in as_completed(futures):
                name = futures[f]
                try:
                    r = f.result()
                except Exception as exc:
                    # Don't let one PDF's crash kill the whole batch. The
                    # script is designed to be re-run, so record the failure
                    # and let the rest finish.
                    r = f"  FAIL-crash  {name}: {type(exc).__name__}: {exc}"
                results.append(r)
                print(r)

    ok = sum(1 for r in results if "ok" in r.lower() and "FAIL" not in r)
    skip = sum(1 for r in results if "skip" in r.lower())
    fail = sum(1 for r in results if "FAIL" in r)
    print(f"\nDone: {ok} processed, {skip} skipped, {fail} failed")

    if fail:
        print("\nFailed PDFs (re-run to retry):")
        for r in results:
            if "FAIL" in r:
                print(f"  {r.strip()}")

    # Persist a run record to Cosmos so dashboards can show "last
    # preanalyze: 47/56 done, 0 errors". Best-effort; a Cosmos failure
    # never fails the actual preanalyze work.
    _try_write_run_record(cfg, args, ok=ok, skipped=skip, failed=fail,
                            results=results)


def _try_write_run_record(cfg: dict, args, ok: int, skipped: int, failed: int,
                            results: list[str]) -> None:
    """Best-effort Cosmos write at end of preanalyze. Imports lazily so a
    missing azure-cosmos dep doesn't break the script."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import cosmos_writer  # noqa: WPS433 — local import is intentional
    except Exception:
        return
    try:
        errors = []
        for r in results:
            if "FAIL" in r:
                # Lines look like "  FAIL-di  manual.pdf: RuntimeError: ..."
                errors.append(r.strip()[:300])
        cosmos_writer.write_run_record(cfg, {
            "run_type": "preanalyze",
            "phase": getattr(args, "phase", "all"),
            "force": bool(getattr(args, "force", False)),
            "incremental": bool(getattr(args, "incremental", False)),
            "pdfs_processed": ok,
            "pdfs_skipped": skipped,
            "pdfs_failed": failed,
            "errors": errors,
        })
    except Exception as exc:
        # Lazy import succeeded but the write failed (network / config /
        # auth). Do NOT fail the run.
        print(f"  warn: cosmos run_history write failed: {exc}", flush=True)


if __name__ == "__main__":
    main()
