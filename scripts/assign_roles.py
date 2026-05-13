"""
Cross-platform RBAC bootstrapper.

Reads deploy.config.json, discovers principal IDs of every managed
identity in the system, and assigns the roles defined in
docs/SETUP.md (RBAC matrix). Idempotent — re-running is safe; existing
assignments are skipped.

Replaces the older PowerShell-only scripts/assign_roles.ps1. Used by
both:
  - Jenkinsfile.deploy   (after Bicep provisioning, before deploy_search.py)
  - One-time bootstrap on a developer machine

Usage:
    python scripts/assign_roles.py --config deploy.config.json
    python scripts/assign_roles.py --config deploy.config.json --dry-run
    python scripts/assign_roles.py --config deploy.config.json --skip-deploy-principal
    python scripts/assign_roles.py --config deploy.config.json --jenkins-principal-id <oid>

What it grants
--------------
A. Deploy principal (the user/SP running this script):
   - Search Service Contributor   on Search service
   - Search Index Data Contributor on Search service
   - Storage Blob Data Contributor on Storage account
   - Cognitive Services OpenAI User on AOAI
   - Cognitive Services User       on Document Intelligence
   - Cosmos DB Built-in Data Contributor on Cosmos DB (if configured)

B. Search service managed identity:
   - Storage Blob Data Reader      on Storage account
   - Cognitive Services OpenAI User on AOAI
   - Cognitive Services User       on AI Services account

C. Function App managed identity:
   - Storage Blob Data Reader      on Storage account
   - Cognitive Services OpenAI User on AOAI
   - Cognitive Services User       on Document Intelligence
   - Search Index Data Reader      on Search service

D. Jenkins agent identity (optional, only with --jenkins-principal-id):
   - Same as deploy principal
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Cosmos data-plane built-in role definition IDs.
# These are NOT regular Azure RBAC roles — they're SQL data-plane role
# definitions and need a different `az` command (cosmosdb sql role).
COSMOS_DATA_CONTRIBUTOR_ROLE_ID = "00000000-0000-0000-0000-000000000002"


def _az_bin() -> str:
    return "az.cmd" if os.name == "nt" else "az"


def az(args: list[str], timeout: float = 60.0, *, check: bool = True) -> str:
    """Run an az CLI command. Raises RuntimeError on non-zero exit if check=True."""
    cmd = [_az_bin()] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"az timed out: {' '.join(args[:6])}...") from exc
    if check and r.returncode != 0:
        raise RuntimeError(
            f"az exit {r.returncode}: {' '.join(args[:6])}\nstderr: {r.stderr.strip()[:500]}"
        )
    return r.stdout.strip()


def _resource_id(account_name: str, rg: str, kind: str) -> str:
    """Return the ARM resource id for a Cognitive Services / Storage /
    Search account. `kind` selects the az subcommand."""
    if kind == "search":
        return az(["search", "service", "show", "-n", account_name, "-g", rg, "--query", "id", "-o", "tsv"])
    if kind == "storage":
        return az(["storage", "account", "show", "-n", account_name, "-g", rg, "--query", "id", "-o", "tsv"])
    if kind == "cognitive":
        return az(["cognitiveservices", "account", "show", "-n", account_name, "-g", rg, "--query", "id", "-o", "tsv"])
    if kind == "functionapp":
        return az(["functionapp", "show", "-n", account_name, "-g", rg, "--query", "id", "-o", "tsv"])
    if kind == "cosmos":
        return az(["cosmosdb", "show", "-n", account_name, "-g", rg, "--query", "id", "-o", "tsv"])
    raise ValueError(f"unknown kind: {kind}")


def _principal_id(name: str, rg: str, kind: str) -> str:
    """Return the principalId of a system-assigned MI."""
    if kind == "search":
        return az(["search", "service", "show", "-n", name, "-g", rg,
                   "--query", "identity.principalId", "-o", "tsv"])
    if kind == "functionapp":
        return az(["functionapp", "identity", "show", "-n", name, "-g", rg,
                   "--query", "principalId", "-o", "tsv"])
    raise ValueError(f"unknown principal kind: {kind}")


def _signed_in_principal() -> tuple[str, str]:
    """Return (object_id, principal_type) for the currently logged-in
    az identity. Works for both users and service principals."""
    raw = az(["account", "show", "--query", "{user:user}", "-o", "json"])
    info = json.loads(raw)
    user = info.get("user") or {}
    principal_type = "ServicePrincipal" if user.get("type") == "servicePrincipal" else "User"

    if principal_type == "User":
        oid = az(["ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"])
    else:
        # SP: az account show gives the appId; need to look up the SP's objectId
        app_id = user.get("name", "")
        if not app_id:
            raise RuntimeError("could not determine signed-in service principal appId")
        oid = az(["ad", "sp", "show", "--id", app_id, "--query", "id", "-o", "tsv"])
    return oid, principal_type


def grant_rbac(principal_id: str, principal_type: str, role: str, scope: str,
               *, dry_run: bool, label: str) -> bool:
    """Idempotent role assignment. Returns True if a new assignment was
    created, False if it already existed or dry-run."""
    if dry_run:
        print(f"  [dry-run] would grant {role} -> {label} on {scope.split('/')[-1]}")
        return False

    # Check existing first to keep output clean.
    existing = az(
        [
            "role", "assignment", "list",
            "--assignee-object-id", principal_id,
            "--role", role,
            "--scope", scope,
            "-o", "json",
        ],
        check=False,
    )
    try:
        if existing and json.loads(existing):
            print(f"  ok   {role} -> {label} (already assigned)")
            return False
    except Exception:
        pass

    az(
        [
            "role", "assignment", "create",
            "--assignee-object-id", principal_id,
            "--assignee-principal-type", principal_type,
            "--role", role,
            "--scope", scope,
            "--only-show-errors",
        ],
        check=False,
    )
    print(f"  +    {role} -> {label}")
    return True


def grant_cosmos_data_role(principal_id: str, cosmos_account: str, rg: str,
                            *, dry_run: bool) -> bool:
    """Cosmos DB data-plane role grants use a different az command.
    The role definition ID 00000000...0002 is the Built-in Data
    Contributor role (read+write to all collections in the account)."""
    if dry_run:
        print(f"  [dry-run] would grant Cosmos DB Data Contributor -> {principal_id[:8]}... on {cosmos_account}")
        return False

    # Check if already assigned.
    existing = az(
        [
            "cosmosdb", "sql", "role", "assignment", "list",
            "--account-name", cosmos_account,
            "--resource-group", rg,
            "-o", "json",
        ],
        check=False,
    )
    try:
        for a in json.loads(existing) if existing else []:
            if a.get("principalId", "").lower() == principal_id.lower():
                print(f"  ok   Cosmos DB Data Contributor -> {cosmos_account} (already assigned)")
                return False
    except Exception:
        pass

    cosmos_id = _resource_id(cosmos_account, rg, "cosmos")
    az(
        [
            "cosmosdb", "sql", "role", "assignment", "create",
            "--account-name", cosmos_account,
            "--resource-group", rg,
            "--scope", cosmos_id,
            "--principal-id", principal_id,
            "--role-definition-id", COSMOS_DATA_CONTRIBUTOR_ROLE_ID,
        ],
        check=False,
    )
    print(f"  +    Cosmos DB Data Contributor -> {cosmos_account}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Cross-platform RBAC bootstrapper")
    ap.add_argument("--config", default="deploy.config.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be granted without changing anything.")
    ap.add_argument("--skip-deploy-principal", action="store_true",
                    help="Don't grant roles to the signed-in principal "
                         "(used when running from Jenkins where the agent already has roles).")
    ap.add_argument("--jenkins-principal-id",
                    help="If set, grant the deploy-principal role set to this object ID "
                         "(for one-time RBAC bootstrap of a Jenkins agent identity).")
    ap.add_argument("--wait-for-propagation", type=int, default=0,
                    help="Sleep this many seconds after assigning to let RBAC propagate "
                         "before exiting. Recommended 300 in CI.")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))

    # Pull resource names from config.
    func_name = cfg["functionApp"]["name"]
    func_rg = cfg["functionApp"]["resourceGroup"]
    storage_rid = cfg["storage"]["accountResourceId"].rstrip("/")
    storage_name = storage_rid.split("/")[-1]
    storage_rg = storage_rid.split("/")[4] if "/resourceGroups/" in storage_rid else func_rg
    search_endpoint = cfg["search"]["endpoint"]
    search_name = search_endpoint.replace("https://", "").split(".")[0]
    aoai_endpoint = cfg["azureOpenAI"]["endpoint"]
    aoai_name = aoai_endpoint.replace("https://", "").split(".")[0]
    di_endpoint = cfg["documentIntelligence"]["endpoint"]
    di_name = di_endpoint.replace("https://", "").split(".")[0]
    aisvc_url = cfg["aiServices"]["subdomainUrl"]
    aisvc_name = aisvc_url.replace("https://", "").split(".")[0]

    cosmos_cfg = cfg.get("cosmos") or {}
    cosmos_endpoint = cosmos_cfg.get("endpoint", "")
    cosmos_name = (
        cosmos_endpoint.replace("https://", "").split(".")[0]
        if cosmos_endpoint else ""
    )

    print(f"Resource group       : {func_rg}")
    print(f"Function App         : {func_name}")
    print(f"Search service       : {search_name}")
    print(f"Storage account      : {storage_name}  (rg: {storage_rg})")
    print(f"AOAI                 : {aoai_name}")
    print(f"Document Intelligence: {di_name}")
    print(f"AI Services          : {aisvc_name}")
    if cosmos_name:
        print(f"Cosmos DB            : {cosmos_name}")
    print()

    print("Looking up resource IDs and principal IDs...")
    storage_id = _resource_id(storage_name, storage_rg, "storage")
    search_id = _resource_id(search_name, func_rg, "search")
    aoai_id = _resource_id(aoai_name, func_rg, "cognitive")
    di_id = _resource_id(di_name, func_rg, "cognitive")
    aisvc_id = _resource_id(aisvc_name, func_rg, "cognitive")

    search_mi = _principal_id(search_name, func_rg, "search")
    func_mi = _principal_id(func_name, func_rg, "functionapp")

    if not search_mi:
        raise SystemExit(
            f"\nABORT: Search service '{search_name}' has no system-assigned MI.\n"
            f"  Enable: az search service update -n {search_name} -g {func_rg} "
            f"--identity-type SystemAssigned"
        )
    if not func_mi:
        raise SystemExit(
            f"\nABORT: Function App '{func_name}' has no system-assigned MI.\n"
            f"  Enable: az functionapp identity assign -n {func_name} -g {func_rg}"
        )

    deploy_oid, deploy_type = _signed_in_principal() if not args.skip_deploy_principal else ("", "")

    granted = 0

    # --- A. Deploy principal grants ---
    if not args.skip_deploy_principal:
        print(f"\nA. Granting deploy principal ({deploy_type} {deploy_oid[:8]}...) roles:")
        for role, scope, label in [
            ("Search Service Contributor",     search_id,  search_name),
            ("Search Index Data Contributor",  search_id,  search_name),
            ("Storage Blob Data Contributor",  storage_id, storage_name),
            ("Cognitive Services OpenAI User", aoai_id,    aoai_name),
            ("Cognitive Services User",        di_id,      di_name),
        ]:
            if grant_rbac(deploy_oid, deploy_type, role, scope, dry_run=args.dry_run, label=label):
                granted += 1
        if cosmos_name:
            if grant_cosmos_data_role(deploy_oid, cosmos_name, func_rg, dry_run=args.dry_run):
                granted += 1
    else:
        print("\nA. Skipping deploy principal grants (--skip-deploy-principal)")

    # --- B. Search service MI grants ---
    print(f"\nB. Granting Search service MI ({search_mi[:8]}...) roles:")
    for role, scope, label in [
        ("Storage Blob Data Reader",       storage_id, storage_name),
        ("Cognitive Services OpenAI User", aoai_id,    aoai_name),
        ("Cognitive Services User",        aisvc_id,   aisvc_name),
    ]:
        if grant_rbac(search_mi, "ServicePrincipal", role, scope, dry_run=args.dry_run, label=label):
            granted += 1

    # --- C. Function App MI grants ---
    print(f"\nC. Granting Function App MI ({func_mi[:8]}...) roles:")
    for role, scope, label in [
        ("Storage Blob Data Reader",       storage_id, storage_name),
        ("Cognitive Services OpenAI User", aoai_id,    aoai_name),
        ("Cognitive Services User",        di_id,      di_name),
        ("Search Index Data Reader",       search_id,  search_name),
    ]:
        if grant_rbac(func_mi, "ServicePrincipal", role, scope, dry_run=args.dry_run, label=label):
            granted += 1
    if cosmos_name:
        if grant_cosmos_data_role(func_mi, cosmos_name, func_rg, dry_run=args.dry_run):
            granted += 1

    # --- D. Optional Jenkins agent grants ---
    if args.jenkins_principal_id:
        print(f"\nD. Granting Jenkins agent ({args.jenkins_principal_id[:8]}...) roles:")
        for role, scope, label in [
            ("Search Service Contributor",     search_id,  search_name),
            ("Search Index Data Contributor",  search_id,  search_name),
            ("Storage Blob Data Contributor",  storage_id, storage_name),
            ("Cognitive Services OpenAI User", aoai_id,    aoai_name),
            ("Cognitive Services User",        di_id,      di_name),
        ]:
            if grant_rbac(args.jenkins_principal_id, "ServicePrincipal", role, scope,
                          dry_run=args.dry_run, label=label):
                granted += 1
        if cosmos_name:
            if grant_cosmos_data_role(args.jenkins_principal_id, cosmos_name, func_rg,
                                        dry_run=args.dry_run):
                granted += 1

    print(f"\n{granted} new assignment(s) created. Existing assignments were left in place.")

    if args.wait_for_propagation > 0 and not args.dry_run and granted > 0:
        print(f"\nWaiting {args.wait_for_propagation}s for RBAC propagation...")
        time.sleep(args.wait_for_propagation)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as e:
        print(f"az command failed: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"ABORT: {e}", file=sys.stderr)
        sys.exit(1)
