#!/usr/bin/env bash
# Publish the Function App code and apply App Settings from deploy.config.json.
# Requires: az CLI (logged in), Azure Functions Core Tools v4, jq.
#
# Usage:
#   scripts/deploy_function.sh [deploy.config.json]

set -euo pipefail

CONFIG="${1:-deploy.config.json}"
if [[ ! -f "$CONFIG" ]]; then
  echo "Config file not found: $CONFIG" >&2
  exit 1
fi

FUNC_APP=$(jq -r '.functionApp.name' "$CONFIG")
RG=$(jq -r '.functionApp.resourceGroup' "$CONFIG")
[[ -z "$FUNC_APP" || -z "$RG" ]] && { echo "functionApp.name and functionApp.resourceGroup must be set in $CONFIG" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Publishing function code to ${FUNC_APP}"
pushd "${REPO_ROOT}/function_app" >/dev/null
# Capture output so we can scan for transient-failure markers that
# `func` doesn't always reflect in its exit code (ServiceUnavailable,
# GatewayTimeout 504, etc.). Bail out before App Settings update if
# the publish actually failed -- the previous behavior silently moved
# on and falsely reported "Function App ready" with old code still live.
PUBLISH_LOG="$(mktemp)"
set +e
func azure functionapp publish "${FUNC_APP}" --python 2>&1 | tee "${PUBLISH_LOG}"
PUBLISH_RC=${PIPESTATUS[0]}
set -e
popd >/dev/null

if [[ $PUBLISH_RC -ne 0 ]] || \
   grep -qE 'Error Uploading archive|ServiceUnavailable|GatewayTimeout|Connection Timed Out|Application Error|Deployment failed' "${PUBLISH_LOG}"; then
  echo ""
  echo "==> ABORT: function publish FAILED" >&2
  echo "    Exit code: $PUBLISH_RC" >&2
  echo "" >&2
  echo "Common causes:" >&2
  echo "  - Azure Functions Kudu transient issue. Wait 2-5 min, restart, retry." >&2
  echo "  - 'az functionapp restart -n ${FUNC_APP} -g ${RG}' and retry." >&2
  echo "  - Local Python version mismatch with function app." >&2
  echo "" >&2
  echo "NOT applying App Settings -- new code isn't live." >&2
  rm -f "${PUBLISH_LOG}"
  exit 1
fi
rm -f "${PUBLISH_LOG}"

echo "==> Applying App Settings"

# Extract values from the config file. Empty values are skipped so the
# script doesn't clobber existing settings with empty strings.
AOAI_ENDPOINT=$(jq -r '.azureOpenAI.endpoint // ""' "$CONFIG")
AOAI_API_VERSION=$(jq -r '.azureOpenAI.apiVersion // "2024-12-01-preview"' "$CONFIG")
AOAI_CHAT=$(jq -r '.azureOpenAI.chatDeployment // ""' "$CONFIG")
AOAI_VISION=$(jq -r '.azureOpenAI.visionDeployment // ""' "$CONFIG")
DI_ENDPOINT=$(jq -r '.documentIntelligence.endpoint // ""' "$CONFIG")
DI_API_VERSION=$(jq -r '.documentIntelligence.apiVersion // "2024-11-30"' "$CONFIG")
SEARCH_ENDPOINT=$(jq -r '.search.endpoint // ""' "$CONFIG")
SEARCH_PREFIX=$(jq -r '.search.artifactPrefix // "mm-manuals"' "$CONFIG")
APPI_CONN=$(jq -r '.appInsights.connectionString // ""' "$CONFIG")
SKILL_VERSION=$(jq -r '.skillVersion // "1.0.0"' "$CONFIG")

# Storage and indexer-name settings used by the auto_heal timer trigger.
STORAGE_ACCT_RESOURCE_ID=$(jq -r '.storage.accountResourceId // ""' "$CONFIG")
STORAGE_ACCT_NAME="${STORAGE_ACCT_RESOURCE_ID##*/}"
STORAGE_CONTAINER=$(jq -r '.storage.pdfContainerName // ""' "$CONFIG")

SETTINGS=(
  "AUTH_MODE=mi"
  "AOAI_ENDPOINT=${AOAI_ENDPOINT}"
  "AOAI_API_VERSION=${AOAI_API_VERSION}"
  "AOAI_CHAT_DEPLOYMENT=${AOAI_CHAT}"
  "AOAI_VISION_DEPLOYMENT=${AOAI_VISION}"
  "DI_ENDPOINT=${DI_ENDPOINT}"
  "DI_API_VERSION=${DI_API_VERSION}"
  "SEARCH_ENDPOINT=${SEARCH_ENDPOINT}"
  "SEARCH_INDEX_NAME=${SEARCH_PREFIX}-index"
  "SEARCH_INDEXER_NAME=${SEARCH_PREFIX}-indexer"
  "STORAGE_ACCOUNT_NAME=${STORAGE_ACCT_NAME}"
  "STORAGE_CONTAINER_NAME=${STORAGE_CONTAINER}"
  "AUTO_HEAL_ENABLED=false"
  "AUTO_HEAL_STUCK_AFTER_MIN=60"
  "AUTO_HEAL_MAX_BLOBS_PER_RUN=20"
  "SKILL_VERSION=${SKILL_VERSION}"
  # Python worker concurrency. Azure Functions Python defaults to 1 process
  # × 1 thread = no parallelism, which causes every skill call to serialize
  # through one worker -- guaranteeing 230s timeouts under any indexer
  # parallelism. 4 processes × 16 threads = 64 concurrent capacity, which
  # comfortably handles dop=6 across 5 per-record skills (30 max concurrent).
  "FUNCTIONS_WORKER_PROCESS_COUNT=4"
  "PYTHON_THREADPOOL_THREAD_COUNT=16"
)

# Only set App Insights if a connection string is provided.
if [[ -n "$APPI_CONN" ]]; then
  SETTINGS+=("APPLICATIONINSIGHTS_CONNECTION_STRING=${APPI_CONN}")
fi

az functionapp config appsettings set \
  -g "${RG}" -n "${FUNC_APP}" \
  --settings "${SETTINGS[@]}" \
  --output none

echo "==> Function App ${FUNC_APP} ready"
