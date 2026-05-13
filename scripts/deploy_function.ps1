param(
  [string]$Config = 'deploy.config.json'
)
$ErrorActionPreference = 'Stop'

if (-not (Test-Path $Config)) { throw "Config file not found: $Config" }
$cfg = Get-Content $Config -Raw | ConvertFrom-Json

$FuncApp = $cfg.functionApp.name
$Rg = $cfg.functionApp.resourceGroup
if (-not $FuncApp -or -not $Rg) { throw 'functionApp.name and functionApp.resourceGroup must be set' }

$RepoRoot = Split-Path -Parent $PSScriptRoot

Write-Host "==> Publishing function code to $FuncApp"
Push-Location (Join-Path $RepoRoot 'function_app')
# Capture both exit code and output. `func` doesn't always set $LASTEXITCODE
# on transient upload failures (ServiceUnavailable, GatewayTimeout 504),
# so we ALSO scan stderr/stdout for known failure markers. Either signal
# aborts the deploy rather than silently continuing to "Function App ready".
$publishOutput = & func azure functionapp publish $FuncApp --python --verbose 2>&1 | Tee-Object -Variable rawOut
$publishExitCode = $LASTEXITCODE
Pop-Location

$publishText = ($publishOutput | Out-String)
$failureMarkers = @(
  'Error Uploading archive',
  'ServiceUnavailable',
  'GatewayTimeout',
  'Connection Timed Out',
  'Application Error',
  'Deployment failed'
)
$failureHit = $failureMarkers | Where-Object { $publishText -match [regex]::Escape($_) }

if ($publishExitCode -ne 0 -or $failureHit) {
  Write-Host ""
  Write-Host "==> ABORT: function publish FAILED" -ForegroundColor Red
  if ($failureHit) {
    Write-Host "    Detected failure marker(s): $($failureHit -join ', ')" -ForegroundColor Red
  }
  Write-Host "    Exit code: $publishExitCode" -ForegroundColor Red
  Write-Host ""
  Write-Host "Common causes (and fixes):" -ForegroundColor Yellow
  Write-Host "  - Azure Functions Kudu is having a transient issue. Wait 2-5 min, restart the function app, retry."
  Write-Host "  - Function app is restarting. Run 'az functionapp restart -n $FuncApp -g $Rg' and retry."
  Write-Host "  - Local Python version mismatch with function app. The warning at the top of this output may matter."
  Write-Host ""
  Write-Host "NOT continuing with App Settings update because the new code isn't live." -ForegroundColor Red
  throw "func azure functionapp publish failed (see output above)"
}

Write-Host "==> Applying App Settings"

$apiVersion = $cfg.azureOpenAI.apiVersion; if (-not $apiVersion) { $apiVersion = '2024-12-01-preview' }
$diApi = $cfg.documentIntelligence.apiVersion; if (-not $diApi) { $diApi = '2024-11-30' }
$prefix = $cfg.search.artifactPrefix; if (-not $prefix) { $prefix = 'mm-manuals' }
$skillVersion = $cfg.skillVersion; if (-not $skillVersion) { $skillVersion = '1.0.0' }

# Storage and indexer-name settings used by the auto_heal timer trigger.
$storageAcctName = ($cfg.storage.accountResourceId -split '/')[-1]

$settings = @(
  "AUTH_MODE=mi",
  "AOAI_ENDPOINT=$($cfg.azureOpenAI.endpoint)",
  "AOAI_API_VERSION=$apiVersion",
  "AOAI_CHAT_DEPLOYMENT=$($cfg.azureOpenAI.chatDeployment)",
  "AOAI_VISION_DEPLOYMENT=$($cfg.azureOpenAI.visionDeployment)",
  "DI_ENDPOINT=$($cfg.documentIntelligence.endpoint)",
  "DI_API_VERSION=$diApi",
  "SEARCH_ENDPOINT=$($cfg.search.endpoint)",
  "SEARCH_INDEX_NAME=$prefix-index",
  "SEARCH_INDEXER_NAME=$prefix-indexer",
  "STORAGE_ACCOUNT_NAME=$storageAcctName",
  "STORAGE_CONTAINER_NAME=$($cfg.storage.pdfContainerName)",
  "AUTO_HEAL_ENABLED=false",   # safe default; set to true once steady-state verified
  "AUTO_HEAL_STUCK_AFTER_MIN=60",
  "AUTO_HEAL_MAX_BLOBS_PER_RUN=20",
  "SKILL_VERSION=$skillVersion",
  # Python worker concurrency. Azure Functions Python defaults to 1 process
  # x 1 thread = no parallelism, which serializes every skill call through
  # one worker -- guaranteeing 230s timeouts under indexer parallelism.
  # 4 processes x 16 threads = 64 concurrent capacity, comfortably handling
  # dop=6 across 5 per-record skills (30 max concurrent).
  "FUNCTIONS_WORKER_PROCESS_COUNT=4",
  "PYTHON_THREADPOOL_THREAD_COUNT=16"
)
if ($cfg.appInsights.connectionString) {
  $settings += "APPLICATIONINSIGHTS_CONNECTION_STRING=$($cfg.appInsights.connectionString)"
}

az functionapp config appsettings set -g $Rg -n $FuncApp --settings $settings --output none

Write-Host "==> Function App $FuncApp ready"
