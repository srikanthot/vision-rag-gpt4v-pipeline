"""
Preflight check for the preanalyze + indexer pipeline.

Runs in under 30 seconds and validates the environment BEFORE the user
starts a multi-hour run. Exits non-zero with a clear message if anything
is off, so the operator can fix it before wasting time.

What this catches (the categories of bugs we've actually hit):
  1. Missing Python packages in the local venv (fitz, httpx, azure-identity)
  2. deploy.config.json missing or missing required keys
  3. Azure CLI not logged in / token expired
  4. Wrong subscription / tenant selected
  5. Blob container doesn't exist or wrong name in config
  6. Function App doesn't exist / is stopped
  7. DI endpoint unreachable from this machine
  8. Import errors in the shared code we rely on

What this does NOT catch (be aware):
  * Runtime errors inside custom skills (only visible at indexer runtime)
  * Vision content-filter false positives
  * DI timeouts on huge PDFs
  * AOAI TPM rate limiting mid-run
  * Transient network failures
  * PDF content that breaks DI parsing
  * Partial cache states (use --status to see those)

Usage:
    python scripts/preflight.py                           # default config
    python scripts/preflight.py --config path/to/cfg.json # custom config
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

# ---------- check helpers ----------

class Check:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.is_warning = False
        self.message = ""
        self.fix = ""

    def ok(self, message: str = "") -> Check:
        self.passed = True
        self.is_warning = False
        self.message = message
        return self

    def fail(self, message: str, fix: str) -> Check:
        self.passed = False
        self.is_warning = False
        self.message = message
        self.fix = fix
        return self

    def warn(self, message: str, fix: str) -> Check:
        """Optional / advisory check. Surfaced to the operator but does
        NOT cause preflight to exit non-zero. Use for things like
        LibreOffice (only matters for non-PDF files)."""
        self.passed = False
        self.is_warning = True
        self.message = message
        self.fix = fix
        return self


def _run_az(args: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    cmd = args[:]
    if cmd and cmd[0] == "az":
        cmd[0] = "az.cmd" if os.name == "nt" else "az"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"command timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", "command not found"


# ---------- individual checks ----------

def check_python_version() -> Check:
    c = Check("Python version")
    v = sys.version_info
    if v.major == 3 and v.minor >= 10:
        return c.ok(f"{v.major}.{v.minor}.{v.micro}")
    return c.fail(
        f"need Python 3.10+, found {v.major}.{v.minor}",
        "Install Python 3.10 or later, then recreate your venv.",
    )


def check_packages() -> Check:
    c = Check("Python packages")
    required = [
        ("fitz", "PyMuPDF"),           # PDF cropping
        ("httpx", "httpx"),             # all HTTP calls
        ("azure.identity", "azure-identity"),  # deploy_search auth
    ]
    missing = []
    for import_name, pip_name in required:
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pip_name)
    if not missing:
        return c.ok("fitz, httpx, azure-identity")
    return c.fail(
        f"missing: {', '.join(missing)}",
        "pip install -r requirements.txt   # from the repo root",
    )


def check_config(config_path: Path) -> tuple[Check, dict | None]:
    c = Check("Config file")
    if not config_path.exists():
        return c.fail(
            f"{config_path} does not exist",
            f"Create {config_path} from deploy.config.example.json and fill in your values.",
        ), None
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return c.fail(
            f"invalid JSON in {config_path}: {e}",
            "Check for trailing commas or unquoted keys; run the file through an online JSON validator.",
        ), None

    required_paths = [
        ("storage", "accountResourceId"),
        ("storage", "pdfContainerName"),
        ("documentIntelligence", "endpoint"),
        ("azureOpenAI", "endpoint"),
        ("azureOpenAI", "visionDeployment"),
        ("azureOpenAI", "embedDeployment"),
        ("functionApp", "name"),
        ("functionApp", "resourceGroup"),
        ("search", "endpoint"),
    ]
    missing = []
    for path in required_paths:
        node: object = cfg
        for key in path:
            if not isinstance(node, dict) or key not in node or not node.get(key):
                missing.append(".".join(path))
                break
            node = node[key]
    if missing:
        return c.fail(
            f"missing/empty keys: {', '.join(missing)}",
            f"Open {config_path} and fill in the missing values.",
        ), cfg
    return c.ok(f"{config_path}"), cfg


def check_az_cli() -> Check:
    c = Check("Azure CLI")
    rc, _, err = _run_az(["az", "--version"], timeout=15.0)
    if rc == 127:
        return c.fail(
            "az not on PATH",
            "Install Azure CLI: https://learn.microsoft.com/cli/azure/install-azure-cli",
        )
    if rc != 0:
        return c.fail(f"az --version failed: {err[:200]}",
                      "Reinstall Azure CLI or check PATH.")
    return c.ok("az on PATH")


def check_az_login() -> tuple[Check, dict | None]:
    c = Check("Azure login")
    rc, out, err = _run_az(["az", "account", "show", "-o", "json"], timeout=15.0)
    if rc != 0:
        return c.fail(
            f"not logged in: {err[:200]}",
            "Run: az login   (and re-run this preflight after)",
        ), None
    try:
        acct = json.loads(out)
    except Exception:
        return c.fail("could not parse `az account show` output", "Run az login."), None
    return c.ok(f"{acct.get('user', {}).get('name', 'unknown')} / {acct.get('name', 'unknown')}"), acct


def check_function_app(cfg: dict) -> Check:
    c = Check("Function App")
    rg = cfg["functionApp"]["resourceGroup"]
    name = cfg["functionApp"]["name"]
    rc, out, err = _run_az([
        "az", "functionapp", "show", "-g", rg, "-n", name,
        "--query", "{state:state,hostname:defaultHostName}", "-o", "json",
    ], timeout=30.0)
    if rc != 0:
        return c.fail(
            f"cannot read function app {name} in {rg}: {err[:200]}",
            f"Verify the function app exists: az functionapp list -g {rg} -o table",
        )
    try:
        info = json.loads(out)
    except Exception:
        return c.fail("could not parse function app info", "Re-run az login and retry.")
    if info.get("state") != "Running":
        return c.fail(
            f"function app state is '{info.get('state')}', not 'Running'",
            f"Start it: az functionapp start -g {rg} -n {name}",
        )
    return c.ok(f"{name} Running @ {info.get('hostname')}")


def check_storage_container(cfg: dict) -> Check:
    c = Check("Blob container")
    account_rid = cfg["storage"]["accountResourceId"]
    container = cfg["storage"]["pdfContainerName"]
    account_name = account_rid.rstrip("/").split("/")[-1]
    rc, _, err = _run_az([
        "az", "storage", "container", "exists",
        "--account-name", account_name, "--name", container,
        "--auth-mode", "login",
        "-o", "json",
    ], timeout=30.0)
    if rc != 0:
        return c.fail(
            f"cannot check container {container} in {account_name}: {err[:200]}",
            "Verify the account name, container name, and that your signed-in "
            "principal has 'Storage Blob Data Contributor' role on the account.",
        )
    return c.ok(f"{account_name}/{container} accessible")


def check_storage_soft_delete(cfg: dict) -> Check:
    """Verify blob soft-delete is enabled on the storage account.
    Reconcile + accidental-delete recovery rely on it. Without it,
    deleting a PDF is irreversible."""
    c = Check("Blob soft-delete enabled")
    account_rid = cfg["storage"]["accountResourceId"]
    account_name = account_rid.rstrip("/").split("/")[-1]
    rc, out, err = _run_az([
        "az", "storage", "account", "blob-service-properties", "show",
        "--account-name", account_name,
        "--query", "deleteRetentionPolicy",
        "-o", "json",
    ], timeout=30.0)
    if rc != 0:
        return c.fail(
            f"cannot read blob-service-properties: {err[:200]}",
            "Need 'Storage Account Contributor' role to read this property.",
        )
    try:
        prop = json.loads(out) if out.strip() else {}
    except Exception:
        prop = {}
    if prop.get("enabled"):
        return c.ok(f"retention {prop.get('days', '?')} days")
    return c.fail(
        "blob soft-delete is OFF",
        f"Enable: az storage account blob-service-properties update "
        f"--account-name {account_name} --enable-delete-retention true "
        f"--delete-retention-days 7",
    )


def check_cosmos(cfg: dict) -> Check:
    """Cosmos is optional. If the cosmos block is in the config, verify
    the endpoint and the agent identity has data-plane access. If it's
    omitted, dashboard features won't work but the pipeline still runs."""
    c = Check("Cosmos DB")
    cosmos_cfg = cfg.get("cosmos") or {}
    endpoint = cosmos_cfg.get("endpoint", "").strip()
    database = cosmos_cfg.get("database", "").strip()
    if not endpoint or not database:
        return c.ok("not configured (dashboard features disabled)")

    account_name = endpoint.split("//")[-1].split(".")[0] if "//" in endpoint else ""
    if not account_name:
        return c.fail(
            f"cosmos.endpoint looks malformed: {endpoint}",
            "Use form: https://<account>.documents.azure.com:443/",
        )
    rg = cfg["functionApp"]["resourceGroup"]

    # Use the modern `az cosmosdb sql database show` instead of the
    # deprecated `az cosmosdb database exists` (which prints a deprecation
    # warning even on success and confuses operators).
    rc, _, err = _run_az([
        "az", "cosmosdb", "sql", "database", "show",
        "--account-name", account_name,
        "--resource-group", rg,
        "--name", database,
    ], timeout=30.0)
    if rc != 0:
        return c.fail(
            f"cannot reach cosmos database '{database}' in {account_name}: {err[:200]}",
            f"If the database doesn't exist yet, create it: "
            f"az cosmosdb sql database create --account-name {account_name} "
            f"--resource-group {rg} --name {database} --throughput 400. "
            f"Otherwise verify cosmos.endpoint and cosmos.database in deploy.config.json.",
        )
    return c.ok(f"{account_name}/{database}")


def check_pipeline_lock_module() -> Check:
    """Verify pipeline_lock can be imported (catches a deploy that
    forgot to copy the file)."""
    c = Check("pipeline_lock module")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        importlib.import_module("pipeline_lock")
    except ImportError as e:
        return c.fail(
            f"cannot import pipeline_lock: {e}",
            "Verify scripts/pipeline_lock.py exists in the repo.",
        )
    return c.ok("imports cleanly")


def check_libreoffice() -> Check:
    """Optional: LibreOffice is required for native PPTX/DOCX/XLSX
    figure extraction. If missing, preanalyze still works for PDFs and
    falls back to text+tables for non-PDFs."""
    c = Check("LibreOffice (optional)")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from convert import is_available
    except ImportError as e:
        return c.fail(
            f"cannot import scripts/convert.py: {e}",
            "Verify scripts/convert.py exists.",
        )
    if is_available():
        return c.ok("present (DOCX/PPTX/XLSX figure extraction enabled)")
    return c.warn(
        "not on PATH (optional, OK to ignore for PDF-only corpora)",
        "Optional. Without LibreOffice, non-PDF files (.docx/.pptx/.xlsx) "
        "are still indexed for text + tables, but their figures are not "
        "analyzed. To enable figure extraction for those formats: "
        "Linux: `sudo apt-get install -y libreoffice` or "
        "`sudo dnf install libreoffice`. "
        "Windows: download from libreoffice.org and ensure soffice.exe "
        "is on PATH or in C:\\Program Files\\LibreOffice\\program\\.",
    )


def check_preanalyze_importable() -> Check:
    """Directly exercise the import that crashed the last live run
    (shared.pdf_crop -> fitz). Mirrors exactly how preanalyze.py
    extends sys.path at runtime."""
    c = Check("preanalyze.py imports")
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root / "function_app"))
    try:
        importlib.import_module("shared.pdf_crop")
    except ImportError as e:
        return c.fail(
            f"cannot import shared.pdf_crop: {e}",
            "pip install -r requirements.txt   (likely the PyMuPDF/fitz gap)",
        )
    return c.ok("shared.pdf_crop + fitz OK")


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="Preflight: validate env before preanalyze")
    ap.add_argument("--config", default="deploy.config.json")
    args = ap.parse_args()

    print()
    print("=" * 60)
    print(" Preflight check")
    print("=" * 60)

    checks: list[Check] = []

    # 1. Python + packages first -- everything else needs them
    checks.append(check_python_version())
    checks.append(check_packages())
    checks.append(check_preanalyze_importable())

    # 2. Config file
    cfg_check, cfg = check_config(Path(args.config))
    checks.append(cfg_check)

    # 3. Azure CLI + login
    az_check = check_az_cli()
    checks.append(az_check)

    if az_check.passed:
        login_check, _ = check_az_login()
        checks.append(login_check)

        # 4. Azure resources (only if logged in + config loaded)
        if login_check.passed and cfg is not None:
            checks.append(check_function_app(cfg))
            checks.append(check_storage_container(cfg))
            checks.append(check_storage_soft_delete(cfg))
            checks.append(check_cosmos(cfg))

    # 5. New-code dependencies
    checks.append(check_pipeline_lock_module())
    checks.append(check_libreoffice())

    # Print results — three states: OK, WARN (advisory, doesn't block),
    # FAIL (hard stop). Warnings are still surfaced so operators see
    # them, but preflight returns 0 when only warnings are present.
    print()
    for c in checks:
        if c.passed:
            tag = "OK  "
        elif c.is_warning:
            tag = "WARN"
        else:
            tag = "FAIL"
        msg = c.message or ""
        print(f"  [{tag}] {c.name:<28s} {msg}")

    failures = [c for c in checks if not c.passed and not c.is_warning]
    warnings = [c for c in checks if c.is_warning]
    print()

    if warnings:
        print(f"{len(warnings)} advisory warning(s) — review but not blocking:")
        for c in warnings:
            print(f"  * {c.name}: {c.message}")
            if c.fix:
                print(f"    note: {c.fix}")
        print()

    if not failures:
        print("All required checks passed. You can run preanalyze now.")
        print()
        return 0

    print(f"{len(failures)} check(s) failed. Fix these before running preanalyze:")
    print()
    for c in failures:
        print(f"  * {c.name}: {c.message}")
        if c.fix:
            print(f"    fix: {c.fix}")
        print()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
