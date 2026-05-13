"""
Diagnose Azure Search 403 errors during deploy_search.py.

Runs four checks in sequence:
  1. Subscription match — is your az CLI in the same sub as the search service?
  2. Identity match — does the Python token's identity match az CLI's user?
  3. Role visibility — are the required roles actually present on this resource?
  4. Actual 403 body — what does Azure Search itself say?

Cross-platform (handles az.cmd on Windows correctly).

Usage:
    python scripts/diagnose_403.py
    python scripts/diagnose_403.py --config path/to/deploy.config.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path


def _az_bin() -> str:
    """Return 'az.cmd' on Windows, 'az' elsewhere — Python's subprocess
    can't find a .cmd file without the .cmd suffix on Windows."""
    return "az.cmd" if os.name == "nt" else "az"


def az(args: list[str], *, check: bool = False) -> str:
    """Run az command and return stdout. Returns empty string on failure
    when check=False (so we can keep the diagnostic running through
    individual command failures)."""
    try:
        r = subprocess.run(
            [_az_bin()] + args,
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        print(f"  ERROR: az CLI not found on PATH. Install Azure CLI first.")
        sys.exit(2)
    if check and r.returncode != 0:
        raise RuntimeError(f"az {' '.join(args[:5])} failed: {r.stderr[:300]}")
    return r.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnose 403 from Azure Search")
    ap.add_argument("--config", default="deploy.config.json")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"ERROR: config file not found: {cfg_path}")
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    endpoint = cfg["search"]["endpoint"].rstrip("/")
    search_name = endpoint.replace("https://", "").split(".")[0]
    rg = cfg["functionApp"]["resourceGroup"]

    print()
    print("=" * 70)
    print("CHECK 1: Subscription match")
    print("=" * 70)
    my_sub = az(["account", "show", "--query", "id", "-o", "tsv"])
    print(f"  az current sub:    {my_sub}")
    res = az([
        "resource", "list",
        "--resource-type", "Microsoft.Search/searchServices",
        "--name", search_name,
        "--query", "[0].id", "-o", "tsv",
    ])
    if res:
        search_sub = res.split("/")[2] if "/subscriptions/" in res else "PARSE_FAIL"
    else:
        search_sub = "NOT_FOUND_IN_CURRENT_SUB"
    print(f"  search service sub: {search_sub}")
    if my_sub and search_sub and my_sub == search_sub:
        print(f"  MATCH: yes -- subs are aligned")
    else:
        print(f"  MATCH: NO  <-- this is your bug")
        print(f"  FIX: az account set --subscription {search_sub}")

    print()
    print("=" * 70)
    print("CHECK 2: Identity Python uses vs az CLI uses")
    print("=" * 70)
    az_user = az(["account", "show", "--query", "user.name", "-o", "tsv"])
    print(f"  az CLI user:       {az_user}")

    try:
        from azure.identity import DefaultAzureCredential
        cred = DefaultAzureCredential()
        token_obj = cred.get_token("https://search.windows.net/.default")
        parts = token_obj.token.split(".")
        pad = "=" * ((4 - len(parts[1]) % 4) % 4)
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
        upn = claims.get("upn") or claims.get("appid")
        oid = claims.get("oid")
        tid = claims.get("tid")
        aud = claims.get("aud")
        print(f"  Python token upn:  {upn}")
        print(f"  Python token oid:  {oid}")
        print(f"  Python token tid:  {tid}")
        print(f"  Python token aud:  {aud}")
        if az_user and upn and az_user.lower() != (upn or "").lower():
            print(f"  MATCH: NO  <-- Python and az CLI are using DIFFERENT identities")
            print(f"  FIX: unset env vars AZURE_CLIENT_ID/AZURE_TENANT_ID/AZURE_CLIENT_SECRET")
        else:
            print(f"  MATCH: yes")
    except ImportError:
        print(f"  ERROR: azure-identity not installed; run: pip install -r requirements.txt")
        return 1
    except Exception as exc:
        print(f"  ERROR fetching Python token: {exc}")
        oid = None
        token_obj = None

    print()
    print("=" * 70)
    print("CHECK 3: Roles ACTUALLY visible on this search service")
    print("=" * 70)
    if not oid:
        print("  SKIPPED -- couldn't get oid from CHECK 2")
    else:
        search_id = az([
            "search", "service", "show",
            "-n", search_name, "-g", rg,
            "--query", "id", "-o", "tsv",
        ])
        if not search_id:
            print(f"  ERROR: search service '{search_name}' not found in RG '{rg}'")
            print(f"  Likely you're in the wrong subscription. See CHECK 1.")
        else:
            print(f"  search id: {search_id}")
            roles = az([
                "role", "assignment", "list",
                "--assignee-object-id", oid,
                "--scope", search_id,
                "--query", "[].roleDefinitionName",
                "-o", "tsv",
            ])
            print(f"  roles found:")
            if not roles:
                print(f"    (NONE)")
                print(f"  FIX: python scripts/assign_roles.py --config {args.config} --wait-for-propagation 300")
            else:
                for line in roles.split("\n"):
                    if line.strip():
                        print(f"    - {line}")
                lines = [line for line in roles.split("\n") if line.strip()]
                needed = {"Search Service Contributor", "Search Index Data Contributor"}
                missing = needed - set(lines)
                if missing:
                    print(f"  MISSING: {missing}")
                    print(f"  FIX: python scripts/assign_roles.py --config {args.config} --wait-for-propagation 300")
                else:
                    print(f"  OK: required roles present")

    print()
    print("=" * 70)
    print("CHECK 4: The actual 403 body")
    print("=" * 70)
    if not token_obj:
        print("  SKIPPED -- no token from CHECK 2")
    else:
        try:
            import httpx
            url = f"{endpoint}/datasources?api-version=2024-11-01-preview"
            r = httpx.get(
                url,
                headers={"Authorization": "Bearer " + token_obj.token},
                timeout=30,
            )
            print(f"  STATUS: {r.status_code}")
            print(f"  BODY (first 1500 chars):")
            for line in r.text[:1500].splitlines()[:40]:
                print(f"    {line}")
        except Exception as exc:
            print(f"  ERROR: {type(exc).__name__}: {exc}")
            print(f"  If SSL error: set $env:SSL_CERT_FILE to your corp CA bundle")

    print()
    print("=" * 70)
    print("WHAT TO DO BASED ON OUTPUT")
    print("=" * 70)
    print("  CHECK 1 MATCH: NO    -> az account set --subscription <correct-sub>")
    print("                          then re-run assign_roles.py")
    print("  CHECK 2 MATCH: NO    -> unset AZURE_CLIENT_* env vars; az logout/login")
    print("  CHECK 3 (NONE)       -> roles aren't on this resource; re-run assign_roles.py")
    print("  CHECK 4 says:")
    print("    'Forbidden by IP firewall'  -> add your IP to search service allowlist")
    print("    'PrincipalNotFound'          -> wait 10-30 min for RBAC propagation")
    print("    'apiKeyOnly'                 -> service is in key-only mode; enable AAD")
    print("    HTML with 'Forcepoint'/etc.  -> corporate proxy blocking *.azure.com")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
