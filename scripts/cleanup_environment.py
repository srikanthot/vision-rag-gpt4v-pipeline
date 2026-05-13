"""
Clean up search artifacts (index, indexer, skillset, datasource) and
optionally the cache + Cosmos containers — useful for fresh-start
testing of bootstrap.py / preanalyze flow.

This DELETES things. Always asks for confirmation unless --yes is set.
NEVER deletes:
  - The Azure resources themselves (Search service, Storage account,
    Function App, etc.) — Bicep owns those
  - The PDFs in your blob container
  - Your role assignments

What it CAN delete:
  - Search index, indexer, skillset, datasource
  - All blobs under _dicache/ (the preanalysis cache)
  - Cosmos DB containers (run_history, pdf_state)

Usage:
    # Preview what would be deleted
    python scripts/cleanup_environment.py --config deploy.config.json --dry-run

    # Delete search artifacts only (keep cache + Cosmos)
    python scripts/cleanup_environment.py --config deploy.config.json --search-only --yes

    # Full cleanup (search + cache + Cosmos containers)
    python scripts/cleanup_environment.py --config deploy.config.json --full --yes
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _az_bin() -> str:
    return "az.cmd" if os.name == "nt" else "az"


def az(args: list[str]) -> tuple[int, str, str]:
    try:
        r = subprocess.run([_az_bin()] + args, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        print("ERROR: az CLI not on PATH")
        sys.exit(2)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def confirm(prompt: str, auto_yes: bool) -> bool:
    if auto_yes:
        return True
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def delete_search_artifacts(cfg: dict, dry_run: bool, auto_yes: bool) -> int:
    endpoint = cfg["search"]["endpoint"].rstrip("/")
    prefix = cfg["search"].get("artifactPrefix") or "mm-manuals"
    artifacts = [
        ("indexers", f"{prefix}-indexer"),
        ("skillsets", f"{prefix}-skillset"),
        ("indexes", f"{prefix}-index"),
        ("datasources", f"{prefix}-ds"),
    ]

    print("\nWill delete these search artifacts:")
    for collection, name in artifacts:
        print(f"  - {collection}/{name}")

    if not dry_run and not confirm("Delete these search artifacts?", auto_yes):
        print("  Skipped.")
        return 0

    if dry_run:
        print("  [dry-run] no changes made")
        return 0

    rc, token, _ = az(["account", "get-access-token", "--resource",
                       "https://search.windows.net", "--query", "accessToken", "-o", "tsv"])
    if rc != 0 or not token:
        print("  ERROR: could not acquire AAD token for Search")
        return 1

    deleted = 0
    for collection, name in artifacts:
        url = f"{endpoint}/{collection}/{name}?api-version=2024-11-01-preview"
        try:
            import httpx
            r = httpx.delete(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
            if r.status_code in (200, 204):
                print(f"  deleted {collection}/{name}")
                deleted += 1
            elif r.status_code == 404:
                print(f"  (not found) {collection}/{name}")
            else:
                print(f"  WARN {collection}/{name}: {r.status_code} {r.text[:100]}")
        except Exception as exc:
            print(f"  ERROR deleting {collection}/{name}: {exc}")
    return 0 if deleted >= 0 else 1


def delete_cache_blobs(cfg: dict, dry_run: bool, auto_yes: bool) -> int:
    storage_rid = cfg["storage"]["accountResourceId"].rstrip("/")
    storage_name = storage_rid.split("/")[-1]
    container = cfg["storage"]["pdfContainerName"]

    print(f"\nWill delete ALL blobs under _dicache/ in {storage_name}/{container}.")
    print("This removes:")
    print("  - DI cache files (.di.json)")
    print("  - Per-figure crop files (.crop.*.json)")
    print("  - Per-figure vision results (.vision.*.json)")
    print("  - Output assembly files (.output.json)")
    print("  - Pipeline lock files")
    print("Source PDFs are NOT touched.")

    if not dry_run and not confirm("Delete all _dicache/ blobs?", auto_yes):
        print("  Skipped.")
        return 0

    if dry_run:
        rc, out, _ = az([
            "storage", "blob", "list",
            "--account-name", storage_name,
            "--container-name", container,
            "--prefix", "_dicache/",
            "--auth-mode", "login",
            "--query", "length(@)", "-o", "tsv",
        ])
        if rc == 0:
            print(f"  [dry-run] would delete {out.strip()} blobs")
        return 0

    rc, _, err = az([
        "storage", "blob", "delete-batch",
        "--account-name", storage_name,
        "--source", container,
        "--pattern", "_dicache/*",
        "--auth-mode", "login",
    ])
    if rc != 0:
        print(f"  ERROR: {err[:300]}")
        return 1
    print("  cache blobs deleted")
    return 0


def delete_cosmos_containers(cfg: dict, dry_run: bool, auto_yes: bool) -> int:
    cosmos_cfg = cfg.get("cosmos") or {}
    endpoint = cosmos_cfg.get("endpoint", "")
    db = cosmos_cfg.get("database", "")
    if not endpoint or not db:
        print("\n  Cosmos DB not configured -- nothing to clean up")
        return 0

    account = endpoint.replace("https://", "").split(".")[0]
    rg = cfg["functionApp"]["resourceGroup"]
    containers = ["indexing_run_history", "indexing_pdf_state"]

    print(f"\nWill delete Cosmos containers in {account}/{db}:")
    for c in containers:
        print(f"  - {c}")
    print("Database itself will NOT be deleted; only the containers.")

    if not dry_run and not confirm("Delete Cosmos containers?", auto_yes):
        print("  Skipped.")
        return 0

    if dry_run:
        print("  [dry-run] no changes made")
        return 0

    for c in containers:
        rc, _, err = az([
            "cosmosdb", "sql", "container", "delete",
            "--account-name", account,
            "--database-name", db,
            "--resource-group", rg,
            "--name", c,
            "--yes",
        ])
        if rc == 0:
            print(f"  deleted {c}")
        else:
            print(f"  WARN {c}: {err[:200]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Clean up search + cache + Cosmos for fresh-start")
    ap.add_argument("--config", default="deploy.config.json")
    ap.add_argument("--search-only", action="store_true", help="Only delete search artifacts")
    ap.add_argument("--cache-only", action="store_true", help="Only delete cache blobs")
    ap.add_argument("--cosmos-only", action="store_true", help="Only delete Cosmos containers")
    ap.add_argument("--full", action="store_true", help="Delete search + cache + Cosmos")
    ap.add_argument("--dry-run", action="store_true", help="Print what would be deleted, no changes")
    ap.add_argument("--yes", action="store_true", help="Skip confirmation prompts")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))

    flags = sum([args.search_only, args.cache_only, args.cosmos_only, args.full])
    if flags == 0:
        print("ERROR: pass one of --search-only, --cache-only, --cosmos-only, --full")
        ap.print_help()
        return 1
    if flags > 1:
        print("ERROR: pass exactly one mode flag")
        return 1

    if args.search_only or args.full:
        rc = delete_search_artifacts(cfg, args.dry_run, args.yes)
        if rc != 0 and not args.full:
            return rc

    if args.cache_only or args.full:
        rc = delete_cache_blobs(cfg, args.dry_run, args.yes)
        if rc != 0 and not args.full:
            return rc

    if args.cosmos_only or args.full:
        rc = delete_cosmos_containers(cfg, args.dry_run, args.yes)
        if rc != 0 and not args.full:
            return rc

    print("\nCleanup complete.")
    print("Next: run 'python scripts/bootstrap.py --config deploy.config.json --auto-fix'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
