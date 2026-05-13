"""
Render the Azure AI Search artifacts (datasource, index, skillset,
indexer) with values from deploy.config.json and PUT them to the target
Search service using AAD auth.

The function key is fetched live from the Function App via az CLI so it
never sits in the config file.

Usage:
    python scripts/deploy_search.py --config deploy.config.json
    python scripts/deploy_search.py --config deploy.config.json --run-indexer

Prerequisites:
    - Azure CLI logged in
    - The signed-in principal has Search Service Contributor +
      Search Index Data Contributor on the target search service
    - azure-identity and httpx installed
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential

REPO_ROOT = Path(__file__).resolve().parent.parent
SEARCH_DIR = REPO_ROOT / "search"
API_VERSION = "2024-11-01-preview"

ARTIFACT_FILES = [
    ("datasources", "datasource", SEARCH_DIR / "datasource.json"),
    ("indexes",     "index",      SEARCH_DIR / "index.json"),
    ("skillsets",   "skillset",   SEARCH_DIR / "skillset.json"),
    ("indexers",    "indexer",    SEARCH_DIR / "indexer.json"),
]


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"config file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def run(cmd: list[str]) -> str:
    # az.cmd on Windows; bare 'az' everywhere else. Without this, Linux
    # Jenkins agents fail with "az.cmd not found".
    if cmd and cmd[0] == "az":
        cmd[0] = "az.cmd" if os.name == "nt" else "az"
    r = subprocess.run(cmd, capture_output=True, text=True, check=True, shell=False)
    return r.stdout.strip()


def function_key(resource_group: str, function_app_name: str) -> str:
    raw = run([
        "az", "functionapp", "keys", "list",
        "-g", resource_group, "-n", function_app_name, "-o", "json",
    ])
    keys = json.loads(raw)
    return keys["functionKeys"].get("default") or next(iter(keys["functionKeys"].values()))


def function_host(resource_group: str, function_app_name: str) -> str:
    raw = run([
        "az", "functionapp", "show",
        "-g", resource_group, "-n", function_app_name,
        "--query", "defaultHostName", "-o", "tsv",
    ])
    return raw.strip()


def render(body: str, mapping: dict[str, str]) -> str:
    out = body
    for placeholder, value in mapping.items():
        out = out.replace(placeholder, value)
    remaining = re.findall(r"<[A-Z_][A-Z0-9_]*>", out)
    if remaining:
        raise SystemExit(
            f"Unrendered placeholders remain: {sorted(set(remaining))}. "
            f"Add a mapping in scripts/deploy_search.py or fill the matching "
            f"field in deploy.config.json."
        )
    return out


def put_artifact(endpoint: str, token: str, collection: str, name: str, body: dict) -> None:
    url = f"{endpoint}/{collection}/{name}?api-version={API_VERSION}"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=60.0) as c:
        resp = c.put(url, json=body, headers=headers)
    if resp.status_code not in (200, 201, 204):
        raise SystemExit(f"PUT {collection}/{name} failed: {resp.status_code} {resp.text[:600]}")
    print(f"  ok  {collection}/{name}  ({resp.status_code})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="deploy.config.json")
    ap.add_argument("--run-indexer", action="store_true")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))

    func_name = cfg["functionApp"]["name"]
    func_rg = cfg["functionApp"]["resourceGroup"]
    prefix = cfg["search"].get("artifactPrefix") or "mm-manuals"
    search_endpoint = cfg["search"]["endpoint"].rstrip("/")

    names = {
        "datasource": f"{prefix}-ds",
        "index":      f"{prefix}-index",
        "skillset":   f"{prefix}-skillset",
        "indexer":    f"{prefix}-indexer",
    }

    print(f"Fetching function host + key for {func_name}")
    fhost = function_host(func_rg, func_name)
    fkey = function_key(func_rg, func_name)

    mapping = {
        "<STORAGE_RESOURCE_ID>":       cfg["storage"]["accountResourceId"],
        "<STORAGE_CONTAINER_NAME>":    cfg["storage"]["pdfContainerName"],
        "<FUNCTION_APP_HOST>":         fhost,
        "<FUNCTION_KEY>":              fkey,
        "<AOAI_ENDPOINT>":             cfg["azureOpenAI"]["endpoint"].rstrip("/"),
        "<AOAI_EMBED_DEPLOYMENT>":     cfg["azureOpenAI"]["embedDeployment"],
        "<AI_SERVICES_SUBDOMAIN_URL>": cfg["aiServices"]["subdomainUrl"].rstrip("/"),
        "<DATASOURCE_NAME>":           names["datasource"],
        "<INDEX_NAME>":                names["index"],
        "<SKILLSET_NAME>":             names["skillset"],
        "<INDEXER_NAME>":              names["indexer"],
    }

    print("Substitutions:")
    for k, v in mapping.items():
        shown = (v[:4] + "…" + v[-4:]) if k == "<FUNCTION_KEY>" else v
        print(f"  {k} -> {shown}")

    print("Acquiring AAD token for Search")
    search_scope = "https://search.windows.net/.default"
    token = DefaultAzureCredential().get_token(search_scope).token

    print(f"PUTting artifacts to {search_endpoint}")
    for collection, key, path in ARTIFACT_FILES:
        rendered = render(path.read_text(encoding="utf-8"), mapping)
        put_artifact(search_endpoint, token, collection, names[key], json.loads(rendered))

    if args.run_indexer:
        print("Triggering indexer run")
        url = f"{search_endpoint}/indexers/{names['indexer']}/run?api-version={API_VERSION}"
        with httpx.Client(timeout=30.0) as c:
            resp = c.post(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code not in (200, 202, 204):
            raise SystemExit(f"indexer run failed: {resp.status_code} {resp.text[:400]}")
        print("  indexer run accepted")

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"az cli call failed:\n  cmd: {e.cmd}\n  stderr: {e.stderr}", file=sys.stderr)
        sys.exit(1)
