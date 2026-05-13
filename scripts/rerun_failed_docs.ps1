# Selectively retry specific failed/skipped blobs in the indexer.
#
# Use case: indexer is stuck in "Success 0 docs" state because some docs
# got marked as failed (e.g., they were in-progress when the 2-hour
# execution wall hit). This script uses the resetdocs API to clear those
# specific docs from the failed items list and re-trigger the indexer.
#
# Already-indexed docs are NOT touched -- they stay in the index as-is.
#
# Usage:
#   .\scripts\rerun_failed_docs.ps1 deploy.config.json
#
# To customize which PDFs to retry: edit the $FailedPdfs array below.

param(
  [string]$Config = 'deploy.config.json'
)
$ErrorActionPreference = 'Stop'

if (-not (Test-Path $Config)) { throw "Config file not found: $Config" }
$cfg = Get-Content $Config -Raw | ConvertFrom-Json

$SearchEp = $cfg.search.endpoint.TrimEnd('/')
$Prefix = $cfg.search.artifactPrefix
$IndexerName = "$Prefix-indexer"
$StorageAcct = ($cfg.storage.accountResourceId -split '/')[-1]
$Container = $cfg.storage.pdfContainerName

# Detect endpoint suffix from storage account URL (Gov vs Public)
$StorageEndpoint = if ($SearchEp -match '\.azure\.us') {
  'blob.core.windows.net'
} else {
  'blob.core.windows.net'
}

# === EDIT THIS LIST: the PDFs you want to re-process ===
$FailedPdfs = @(
  'ED-DC-CDS.pdf',
  'ED-DC-PEP.pdf',
  'ED-DC-OHC.pdf',
  'ED-ED-OTC.pdf',
  'ED-ED-UGC.pdf',
  'ED-EM-MWM.pdf',
  'ED-EM-SSM.pdf',
  'ED-EO-OPM.pdf',
  'ED-EO-RTM.pdf',
  'ED-EO-SSO.pdf',
  'GD-AS-ATM.pdf',
  'GD-GD-GDS.pdf',
  'GD-GD-GDS_CMI.pdf',
  'GD-GD-GEP.pdf'
)

# Build blob URLs (these are the datasource document IDs for blob indexers)
$BlobUrls = $FailedPdfs | ForEach-Object {
  "https://${StorageAcct}.${StorageEndpoint}/${Container}/${_}"
}

Write-Host "==> Resetting $($BlobUrls.Count) documents in indexer $IndexerName"
$BlobUrls | ForEach-Object { Write-Host "  - $_" }

# Get AAD token for Search service
$Token = az account get-access-token --resource https://search.windows.net --query accessToken -o tsv
if (-not $Token) { throw "Failed to acquire AAD token for Search service" }

# Call resetdocs API
$ResetUrl = "$SearchEp/indexers/$IndexerName/resetdocs?api-version=2024-05-01-preview"
$Body = @{ datasourceDocumentIds = $BlobUrls } | ConvertTo-Json -Depth 3

try {
  $Response = Invoke-RestMethod -Uri $ResetUrl -Method POST `
    -Headers @{ "Authorization" = "Bearer $Token"; "Content-Type" = "application/json" } `
    -Body $Body
  Write-Host "==> Reset accepted by Azure Search" -ForegroundColor Green
} catch {
  Write-Host "Reset failed: $($_.Exception.Message)" -ForegroundColor Red
  if ($_.Exception.Response) {
    $Reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
    Write-Host $Reader.ReadToEnd()
  }
  throw
}

Write-Host ""
Write-Host "==> Triggering indexer run"
$RunUrl = "$SearchEp/indexers/$IndexerName/run?api-version=2024-05-01-preview"
Invoke-RestMethod -Uri $RunUrl -Method POST `
  -Headers @{ "Authorization" = "Bearer $Token"; "Content-Length" = "0" }

Write-Host "==> Indexer run triggered" -ForegroundColor Green
Write-Host ""
Write-Host "Monitor with:"
Write-Host "  python scripts/check_index.py --config $Config --coverage"
