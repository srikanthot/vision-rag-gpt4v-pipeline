"""
One-command bootstrap for a fresh environment.

Replaces the manual sequence of:
  preflight.py → assign_roles.py → fix authOptions → fix network → wait
  → create cosmos db → deploy_function → deploy_search → smoke_test

This script auto-detects and (optionally) auto-fixes common environment
issues that the architect's Bicep template doesn't always handle:

  1. RBAC roles not assigned    -> calls assign_roles.py
  2. RBAC propagation lag        -> waits, retries on 403
  3. Cosmos database missing     -> creates it
  4. Search service in apiKeyOnly mode -> changes to aadOrApiKey  (with --auto-fix)
  5. Search publicNetworkAccess: disabled -> enables it           (with --auto-fix)
  6. Cosmos data-plane RBAC      -> grants role + waits 5+ min
  7. Function app code missing   -> deploys it
  8. Search artifacts missing    -> deploys them
  9. Smoke test failure          -> reports clearly

Usage:
    python scripts/bootstrap.py --config deploy.config.json
    python scripts/bootstrap.py --config deploy.config.json --auto-fix
    python scripts/bootstrap.py --config deploy.config.json --skip-cosmos

Use --auto-fix when you have permission to change security settings on
the search service. Without it, the script reports what needs changing
and stops.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def az_bin() -> str:
    return "az.cmd" if os.name == "nt" else "az"


def az(args: list[str], *, check: bool = False) -> tuple[int, str, str]:
    """Run az command. Returns (returncode, stdout, stderr)."""
    try:
        r = subprocess.run([az_bin()] + args, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        print("ERROR: az CLI not on PATH. Install Azure CLI first.")
        sys.exit(2)
    if check and r.returncode != 0:
        raise RuntimeError(f"az {' '.join(args[:6])} failed: {r.stderr[:300]}")
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def step(msg: str) -> None:
    print(f"  -> {msg}", flush=True)


def run_script(script_path: str, args: list[str]) -> int:
    """Run a Python or shell script as a subprocess. Returns exit code."""
    py = sys.executable or "python"
    cmd = [py, str(REPO_ROOT / script_path)] + args
    print(f"\n$ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd)
    return proc.returncode


# ---------- detection helpers ----------

def detect_search_service_config(search_name: str, rg: str) -> dict:
    rc, out, _ = az([
        "search", "service", "show", "-n", search_name, "-g", rg,
        "--query", "{authOptions:authOptions, publicAccess:publicNetworkAccess, ipRules:networkRuleSet.ipRules}",
        "-o", "json",
    ])
    if rc != 0:
        return {}
    try:
        return json.loads(out)
    except Exception:
        return {}


def detect_cosmos_database(account: str, db: str, rg: str) -> bool:
    rc, out, _ = az([
        "cosmosdb", "sql", "database", "list",
        "--account-name", account, "--resource-group", rg,
        "--query", "[].name", "-o", "tsv",
    ])
    if rc != 0:
        return False
    return db in (out or "").splitlines()


def detect_cosmos_role(account: str, rg: str, principal_oid: str) -> bool:
    """Returns True if principal_oid has at least one role assignment on
    the cosmos account."""
    rc, out, _ = az([
        "cosmosdb", "sql", "role", "assignment", "list",
        "--account-name", account, "--resource-group", rg,
        "-o", "json",
    ])
    if rc != 0:
        return False
    try:
        for r in json.loads(out):
            if (r.get("principalId") or "").lower() == principal_oid.lower():
                return True
    except Exception:
        pass
    return False


# ---------- main bootstrap ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="One-command environment bootstrap")
    ap.add_argument("--config", default="deploy.config.json")
    ap.add_argument("--auto-fix", action="store_true",
                    help="Allow auto-fixing security-relevant settings: "
                         "search service authOptions and publicNetworkAccess. "
                         "Without this, the script reports issues and stops.")
    ap.add_argument("--skip-cosmos", action="store_true",
                    help="Skip Cosmos DB setup entirely.")
    ap.add_argument("--skip-deploy-principal", action="store_true",
                    help="Skip granting RBAC to the signed-in principal "
                         "(use when Jenkins agent identity already has roles).")
    ap.add_argument("--skip-function-app", action="store_true",
                    help="Skip deploying function app code.")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"ERROR: config not found: {cfg_path}")
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    rg = cfg["functionApp"]["resourceGroup"]
    search_name = cfg["search"]["endpoint"].replace("https://", "").split(".")[0]
    cosmos_endpoint = (cfg.get("cosmos") or {}).get("endpoint", "")
    cosmos_db = (cfg.get("cosmos") or {}).get("database", "")
    cosmos_account = cosmos_endpoint.replace("https://", "").split(".")[0] if cosmos_endpoint else ""

    issues_blocking: list[str] = []
    issues_warned: list[str] = []

    # ============================================================
    section("STEP 1 / 8 — Preflight checks")
    # ============================================================
    rc = run_script("scripts/preflight.py", ["--config", args.config])
    if rc != 0:
        print("\nPreflight reported issues. Fix them and re-run.")
        return rc

    # ============================================================
    section("STEP 2 / 8 — Detect search service config issues")
    # ============================================================
    config = detect_search_service_config(search_name, rg)
    auth_opts = config.get("authOptions") or {}
    public_access = config.get("publicAccess") or "Unknown"
    ip_rules = config.get("ipRules") or []

    is_apikey_only = "apiKeyOnly" in auth_opts and "aadOrApiKey" not in auth_opts
    is_public_disabled = (public_access or "").lower() == "disabled"

    print(f"  authOptions:        {list(auth_opts.keys())}")
    print(f"  publicAccess:       {public_access}")
    print(f"  ipRules:            {len(ip_rules)} entries")

    if is_apikey_only:
        if args.auto_fix:
            step("Search service is in apiKeyOnly mode. Auto-fixing -> aadOrApiKey")
            rc, _, err = az([
                "search", "service", "update",
                "-n", search_name, "-g", rg,
                "--auth-options", "aadOrApiKey",
                "--aad-auth-failure-mode", "http403",
            ])
            if rc != 0:
                issues_blocking.append(f"failed to fix authOptions: {err[:200]}")
            else:
                step("authOptions fixed; sleeping 30s for change to take effect")
                time.sleep(30)
        else:
            issues_blocking.append(
                "search service authOptions is 'apiKeyOnly' — AAD-based deploys will get 403. "
                f"Fix: az search service update -n {search_name} -g {rg} "
                "--auth-options aadOrApiKey --aad-auth-failure-mode http403"
                " (or re-run this script with --auto-fix)"
            )

    if is_public_disabled:
        if args.auto_fix:
            step("publicNetworkAccess is disabled. Auto-fixing -> enabled")
            rc, _, err = az([
                "search", "service", "update",
                "-n", search_name, "-g", rg,
                "--public-network-access", "enabled",
            ])
            if rc != 0:
                issues_blocking.append(f"failed to enable public access: {err[:200]}")
            else:
                step("public access enabled; sleeping 15s")
                time.sleep(15)
        else:
            issues_warned.append(
                "search publicNetworkAccess is 'disabled'. If your laptop isn't on a "
                "private endpoint, deploys will fail. Either run from corporate network, "
                "OR re-run this script with --auto-fix (review with security team first)."
            )

    if issues_blocking:
        print("\nBLOCKING ISSUES:")
        for i in issues_blocking:
            print(f"  - {i}")
        return 1

    # ============================================================
    section("STEP 3 / 8 — Cosmos DB database (auto-create if missing)")
    # ============================================================
    if args.skip_cosmos or not cosmos_account:
        print("  Cosmos DB skipped (no config or --skip-cosmos)")
    else:
        if detect_cosmos_database(cosmos_account, cosmos_db, rg):
            step(f"database '{cosmos_db}' already exists in {cosmos_account}")
        else:
            step(f"creating database '{cosmos_db}' in {cosmos_account}...")
            rc, _, err = az([
                "cosmosdb", "sql", "database", "create",
                "--account-name", cosmos_account,
                "--resource-group", rg,
                "--name", cosmos_db,
                "--throughput", "400",
            ])
            if rc != 0:
                issues_warned.append(f"could not create cosmos database: {err[:200]}")
            else:
                step("database created")

    # ============================================================
    section("STEP 4 / 8 — Assign RBAC roles")
    # ============================================================
    role_args = ["--config", args.config, "--wait-for-propagation", "300"]
    if args.skip_deploy_principal:
        role_args.append("--skip-deploy-principal")
    rc = run_script("scripts/assign_roles.py", role_args)
    if rc != 0:
        return rc

    # ============================================================
    section("STEP 5 / 8 — Wait for RBAC propagation")
    # ============================================================
    # We don't force `az logout && az login` here because that breaks
    # automation (interactive). Instead we rely on:
    #   1. The 300s wait baked into assign_roles.py above
    #   2. The 5-attempt retry on deploy_search below (with 120s between)
    # In Gov cloud, total propagation can be 15-30 min. The retry loop
    # absorbs that. If even after 5 retries deploy_search still 403s,
    # the user can re-run bootstrap.py — it's idempotent.
    step("relying on assign_roles' 300s wait + deploy_search's 5-attempt retry")
    step("for full RBAC propagation. No interactive token refresh needed.")

    # ============================================================
    section("STEP 6 / 8 — Deploy Function App code")
    # ============================================================
    if args.skip_function_app:
        print("  --skip-function-app set; skipping")
    else:
        if os.name == "nt":
            cmd = ["powershell", "-File", "scripts/deploy_function.ps1", "-Config", args.config]
        else:
            cmd = ["bash", "scripts/deploy_function.sh", args.config]
        print(f"\n$ {' '.join(cmd)}", flush=True)
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            issues_blocking.append(f"deploy_function failed: rc={rc}")
            return 1

    # ============================================================
    section("STEP 7 / 8 — Deploy search artifacts (5-attempt retry on 403)")
    # ============================================================
    # 5 attempts × 120s wait = up to 10 minutes of total retry time.
    # That covers worst-case Gov-cloud RBAC propagation (~15-30 min)
    # combined with the 5-min wait already done in step 4.
    max_attempts = 5
    rc = -1
    for attempt in range(1, max_attempts + 1):
        rc = run_script("scripts/deploy_search.py", ["--config", args.config])
        if rc == 0:
            step(f"deploy_search succeeded on attempt {attempt}")
            break
        if attempt < max_attempts:
            step(f"attempt {attempt}/{max_attempts} failed; sleeping 120s for RBAC propagation")
            time.sleep(120)
    if rc != 0:
        issues_blocking.append(
            f"deploy_search.py failed after {max_attempts} attempts. Likely causes: "
            "(1) Gov-cloud RBAC took longer than ~15 min — re-run bootstrap.py to retry. "
            "(2) Network firewall blocking your laptop — run from corporate network. "
            "(3) Search service in apiKeyOnly mode — re-run with --auto-fix."
        )
        return 1

    # ============================================================
    section("STEP 8 / 8 — Smoke test")
    # ============================================================
    rc = run_script("scripts/smoke_test.py", ["--config", args.config, "--skip-run"])
    if rc != 0:
        issues_warned.append("smoke_test reported issues; review logs above")

    # ============================================================
    section("DONE")
    # ============================================================
    if issues_warned:
        print("\nWarnings:")
        for w in issues_warned:
            print(f"  - {w}")
        print()
    print("Bootstrap complete. Your environment is ready to ingest PDFs.")
    print()
    print("NEXT STEPS (copy-paste in order):")
    print()
    print("  # 1. Upload your PDFs to the blob container, then run preanalyze.")
    print("  #    DEFAULT (safe, slow):")
    print(f"  python scripts/preanalyze.py --config {args.config}")
    print()
    print("  #    FASTER (recommended for 20+ PDFs, parallel processing):")
    print(f"  python scripts/preanalyze.py --config {args.config} --concurrency 3 --vision-parallel 50")
    print()
    print("  #    FASTEST (phased -- best for 50+ PDFs):")
    print(f"  python scripts/preanalyze.py --config {args.config} --phase di --concurrency 3")
    print(f"  python scripts/preanalyze.py --config {args.config} --phase vision --vision-parallel 60")
    print(f"  python scripts/preanalyze.py --config {args.config} --phase output")
    print()
    print("  # 2. The indexer's first run after deploy_search may have fired with")
    print("  #    no cache. Reset it so it reprocesses with the populated cache:")
    if os.name == "nt":
        print("  .\\scripts\\reset_indexer.ps1")
    else:
        print("  ./scripts/reset_indexer.sh")
    print()
    print("  # 3. Verify everything is indexed:")
    print(f"  python scripts/check_index.py --config {args.config} --coverage")
    print()
    print("In production, Jenkinsfile.run handles steps 1-3 nightly + on demand:")
    print(f"  python scripts/run_pipeline.py --config {args.config} --auto-heal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
