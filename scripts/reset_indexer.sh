#!/usr/bin/env bash
# Reset and re-run the indexer so previously-failed/skipped PDFs reprocess.
#
# Typical use: after running preanalyze for PDFs that were uploaded
# without a pre-analysis pass (indexed with processing_status="needs_preanalyze"),
# call this script to force a fresh indexer pass.
#
# What "reset" does: clears Azure Search's change-tracking state so the
# indexer reprocesses every blob in the container on the next run. It
# does NOT delete existing index documents -- those are upserted by key
# on the next run.
#
# Usage:
#   ./scripts/reset_indexer.sh
#   ./scripts/reset_indexer.sh --config /path/to/config.json
#   ./scripts/reset_indexer.sh --indexer-name my-indexer-name

set -euo pipefail

CONFIG="deploy.config.json"
INDEXER_NAME=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            CONFIG="$2"
            shift 2
            ;;
        --indexer-name)
            INDEXER_NAME="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '2,16p' "$0"
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if [[ ! -f "$CONFIG" ]]; then
    echo "config file not found: $CONFIG" >&2
    exit 1
fi

# Need either jq or python to parse the JSON config. Prefer jq since it's
# a single binary; fall back to python if jq isn't available on the agent.
parse_json() {
    local path="$1"
    if command -v jq >/dev/null 2>&1; then
        jq -r "$path" "$CONFIG"
    else
        python3 - <<PYEOF
import json, sys
cfg = json.load(open("$CONFIG"))
keys = "$path".lstrip(".").split(".")
v = cfg
for k in keys:
    if v is None:
        break
    v = v.get(k) if isinstance(v, dict) else None
print(v if v is not None else "")
PYEOF
    fi
}

SEARCH_ENDPOINT="$(parse_json '.search.endpoint')"
SEARCH_ENDPOINT="${SEARCH_ENDPOINT%/}"
API_VERSION="2024-11-01-preview"

if [[ -z "$INDEXER_NAME" ]]; then
    PREFIX="$(parse_json '.search.artifactPrefix')"
    PREFIX="${PREFIX:-mm-manuals}"
    INDEXER_NAME="${PREFIX}-indexer"
fi

# Azure scope. Matches the rest of the repo.
SCOPE="https://search.windows.net"

echo "Resetting indexer '$INDEXER_NAME' at $SEARCH_ENDPOINT"

az rest --method post \
    --url "$SEARCH_ENDPOINT/indexers/$INDEXER_NAME/reset?api-version=$API_VERSION" \
    --resource "$SCOPE" >/dev/null
echo "Reset: OK"

az rest --method post \
    --url "$SEARCH_ENDPOINT/indexers/$INDEXER_NAME/run?api-version=$API_VERSION" \
    --resource "$SCOPE" >/dev/null
echo "Run triggered: OK"

echo ""
echo "Watch progress: Search service -> Indexers -> $INDEXER_NAME -> Execution history"
