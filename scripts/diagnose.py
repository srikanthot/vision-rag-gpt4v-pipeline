"""
Diagnose function app: check settings, test endpoint, check indexer status.

Usage:
    python scripts/diagnose.py --config deploy.config.json
    python scripts/diagnose.py --config deploy.config.json --cert /path/to/ca.crt
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx


def az(args: list[str]) -> str:
    # az.cmd on Windows, az on Linux/Mac. The same cross-platform check
    # preanalyze.py and deploy_search.py use; required for Linux Jenkins
    # agents.
    az_bin = "az.cmd" if os.name == "nt" else "az"
    cmd = [az_bin] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"  az error: {r.stderr.strip()[:300]}", file=sys.stderr)
        return ""
    return r.stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="deploy.config.json")
    ap.add_argument("--cert", default=None, help="Path to CA bundle for TLS verification (or set SSL_CERT_FILE env var)")
    args = ap.parse_args()

    verify: str | bool = args.cert or os.environ.get("SSL_CERT_FILE") or True

    cfg = json.loads(Path(args.config).read_text())
    rg = cfg["functionApp"]["resourceGroup"]
    fn = cfg["functionApp"]["name"]
    search_ep = cfg["search"]["endpoint"].rstrip("/")
    prefix = cfg["search"].get("artifactPrefix", "mm-manuals")

    print("=" * 60)
    print("1. FUNCTION APP STATUS")
    print("=" * 60)

    host = az(["functionapp", "show", "-g", rg, "-n", fn, "--query", "defaultHostName", "-o", "tsv"])
    print(f"  Hostname: {host}")

    state = az(["functionapp", "show", "-g", rg, "-n", fn, "--query", "state", "-o", "tsv"])
    print(f"  State: {state}")

    # List functions
    funcs_raw = az(["functionapp", "function", "list", "-g", rg, "-n", fn, "-o", "json"])
    if funcs_raw:
        funcs = json.loads(funcs_raw)
        func_names = [f.get("name", "?").split("/")[-1] for f in funcs]
        print(f"  Registered functions ({len(func_names)}): {func_names}")
    else:
        print("  WARNING: Could not list functions (may indicate startup failure)")

    print()
    print("=" * 60)
    print("2. APP SETTINGS CHECK")
    print("=" * 60)

    settings_raw = az(["functionapp", "config", "appsettings", "list", "-g", rg, "-n", fn, "-o", "json"])
    if settings_raw:
        settings = {s["name"]: s["value"] for s in json.loads(settings_raw)}
        required = [
            "AUTH_MODE", "DI_ENDPOINT", "DI_API_VERSION",
            "AOAI_ENDPOINT", "AOAI_CHAT_DEPLOYMENT", "AOAI_VISION_DEPLOYMENT",
            "AOAI_EMBED_DEPLOYMENT", "SEARCH_ENDPOINT", "SEARCH_INDEX_NAME",
            "SKILL_VERSION", "FUNCTIONS_WORKER_RUNTIME",
        ]
        for name in required:
            val = settings.get(name, "")
            if val:
                # Redact sensitive values
                if "KEY" in name or "SECRET" in name:
                    print(f"  OK   {name} = [set]")
                else:
                    print(f"  OK   {name} = {val}")
            else:
                print(f"  MISSING  {name}")

        # Show all settings names
        print(f"\n  All settings: {sorted(settings.keys())}")
    else:
        print("  WARNING: Could not fetch app settings")

    print()
    print("=" * 60)
    print("3. FUNCTION ENDPOINT TEST")
    print("=" * 60)

    if host:
        fkey_raw = az(["functionapp", "keys", "list", "-g", rg, "-n", fn, "-o", "json"])
        if fkey_raw:
            keys = json.loads(fkey_raw)
            fkey = keys.get("functionKeys", {}).get("default", "")
            if fkey:
                # Test process-document with a small dummy payload
                container = cfg["storage"]["pdfContainerName"]
                account_rid = cfg["storage"]["accountResourceId"]
                account_name = account_rid.rstrip("/").split("/")[-1]
                test_url = f"https://{host}/api/process-document?code={fkey}"
                test_body = {
                    "values": [{
                        "recordId": "test-0",
                        "data": {
                            "source_file": "test.pdf",
                            "source_path": f"https://{account_name}.blob.core.windows.net/{container}/test.pdf"
                        }
                    }]
                }
                print(f"  Testing: POST {test_url[:80]}...")
                try:
                    with httpx.Client(verify=verify, timeout=300.0) as c:
                        resp = c.post(test_url, json=test_body)
                        print(f"  HTTP {resp.status_code}")
                        body = resp.text
                        if len(body) > 2000:
                            print(f"  Response (first 2000 chars): {body[:2000]}")
                        else:
                            print(f"  Response: {body}")
                except Exception as e:
                    print(f"  ERROR: {type(e).__name__}: {e}")
            else:
                print("  No function key found")
        else:
            print("  Could not fetch function keys")

    print()
    print("=" * 60)
    print("4. INDEXER STATUS")
    print("=" * 60)

    indexer_name = f"{prefix}-indexer"
    search_scope = "https://search.windows.net/.default"
    try:
        from azure.identity import AzureCliCredential
        cred = AzureCliCredential()
        token = cred.get_token(search_scope).token

        with httpx.Client(verify=verify, timeout=30) as c:
            url = f"{search_ep}/indexers/{indexer_name}/status?api-version=2024-11-01-preview"
            r = c.get(url, headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 200:
                status = r.json()
                last = status.get("lastResult", {})
                print(f"  Overall status: {status.get('status')}")
                print(f"  Last run status: {last.get('status')}")
                print(f"  Last run start: {last.get('startTime')}")
                print(f"  Last run end: {last.get('endTime')}")
                print(f"  Items processed: {last.get('itemsProcessed')}")
                print(f"  Items failed: {last.get('itemsFailed')}")

                errors = last.get("errors", [])
                if errors:
                    print(f"\n  ERRORS ({len(errors)}):")
                    for i, err in enumerate(errors[:10]):
                        print(f"    [{i}] key={err.get('key', '?')[:60]}")
                        print(f"        msg={err.get('errorMessage', '?')[:200]}")
                        details = err.get("details", "")
                        if details:
                            print(f"        details={details[:300]}")
                else:
                    print("  No errors")

                warnings = last.get("warnings", [])
                if warnings:
                    print(f"\n  WARNINGS ({len(warnings)}):")
                    for i, w in enumerate(warnings[:5]):
                        print(f"    [{i}] {w.get('message', '?')[:200]}")
            else:
                print(f"  HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")

    print()
    print("=" * 60)
    print("5. FUNCTION APP LOG STREAM (last errors)")
    print("=" * 60)

    log_raw = az(["webapp", "log", "tail", "-g", rg, "-n", fn, "--timeout", "5"])
    if log_raw:
        # Filter for error/exception lines
        lines = log_raw.split("\n")
        error_lines = [line for line in lines if any(w in line.lower() for w in ["error", "exception", "traceback", "failed"])]
        if error_lines:
            for line in error_lines[-20:]:
                print(f"  {line}")
        else:
            print("  No error lines found in recent logs")
    else:
        print("  Could not fetch logs (try checking App Insights in the portal)")

    print("\nDone.")


if __name__ == "__main__":
    main()
