# 1. Pull latest
git pull indexv05 main

# 2. Verify you're at c83bd2d
git log -1 --oneline

# 3. Set the function app for proper concurrency capacity
$FUNC_APP = "azureindex-functionv05"
$FUNC_RG  = "<your-function-rg>"
az functionapp config appsettings set -n $FUNC_APP -g $FUNC_RG --settings `
  FUNCTIONS_WORKER_PROCESS_COUNT=4 `
  PYTHON_THREADPOOL_THREAD_COUNT=16

# 4. Deploy function app code
.\scripts\deploy_function.ps1 deploy.config.json

# 5. Wait 60 sec, then sanity-check function is responding
$FKEY  = az functionapp keys list -n $FUNC_APP -g $FUNC_RG --query 'functionKeys.default' -o tsv
$FHOST = az functionapp show -n $FUNC_APP -g $FUNC_RG --query 'defaultHostName' -o tsv
curl -X POST "https://${FHOST}/api/process-document?code=${FKEY}" -H "Content-Type: application/json" -d '{\"values\":[]}'
# Expected: {"values": []} with HTTP 200

# 6. Deploy search artifacts (auto-fires indexer)
python scripts/deploy_search.py --config deploy.config.json
