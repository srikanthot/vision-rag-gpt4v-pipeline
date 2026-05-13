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
#   ./scripts/reset_indexer.ps1
#   ./scripts/reset_indexer.ps1 -IndexerName my-indexer-name

param(
    [string]$Config = "deploy.config.json",
    [string]$IndexerName = ""
)

if (-not (Test-Path $Config)) {
    Write-Host "Config file not found: $Config" -ForegroundColor Red
    exit 1
}

$cfg = Get-Content $Config -Raw | ConvertFrom-Json
$searchEndpoint = $cfg.search.endpoint.TrimEnd("/")
$apiVersion = "2024-11-01-preview"

if (-not $IndexerName) {
    $prefix = if ($cfg.search.artifactPrefix) { $cfg.search.artifactPrefix } else { "mm-manuals" }
    $IndexerName = "$prefix-indexer"
}

$scope = "https://search.windows.net/.default"

Write-Host "Resetting indexer '$IndexerName' at $searchEndpoint"

# Reset
az rest --method post `
    --url "$searchEndpoint/indexers/$IndexerName/reset?api-version=$apiVersion" `
    --resource "https://search.windows.net" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Reset failed" -ForegroundColor Red
    exit $LASTEXITCODE
}
Write-Host "Reset: OK" -ForegroundColor Green

# Run
az rest --method post `
    --url "$searchEndpoint/indexers/$IndexerName/run?api-version=$apiVersion" `
    --resource "https://search.windows.net" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Run trigger failed" -ForegroundColor Red
    exit $LASTEXITCODE
}
Write-Host "Run triggered: OK" -ForegroundColor Green
Write-Host ""
Write-Host "Watch progress in Azure portal: Search service -> Indexers -> $IndexerName -> Execution history"
