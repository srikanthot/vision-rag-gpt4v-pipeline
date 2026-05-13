# Force-retry specific blobs by bumping their lastModified timestamp.
#
# More aggressive than rerun_failed_docs.ps1: this script also UPDATES
# the blob's metadata, which forces lastModified to advance to "now".
# That bypasses ALL of Azure Search's change-tracking state -- failed
# items list, high-water-mark, etc. -- because the blob now appears as
# a brand-new modified blob.
#
# Use when rerun_failed_docs.ps1 alone didn't trigger reprocessing.
#
# Already-indexed docs are NOT touched -- they stay in the index as-is
# with their existing records, until their respective blob is also
# touched OR the indexer naturally re-processes them on a future change.
#
# Usage:
#   .\scripts\force_reindex_blobs.ps1 deploy.config.json
#
# To customize which PDFs: edit the $FailedPdfs array below.

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

# === EDIT THIS LIST: the PDFs you want to re-process ===
$FailedPdfs = @(
  'ED-DC-CDS.pdf',
  'ED-DC-PEP.pdf',
  'ED-ED-OHC.pdf',
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

# Detect storage endpoint suffix (Gov vs Public)
$StorageEndpoint = if ($SearchEp -match '\.azure\.us') {
  'blob.core.windows.net'
} else {
  'blob.core.windows.net'
}

# Step 1: Update blob metadata to bump lastModified
# Locally relax ErrorActionPreference so one missing blob (typo etc.)
# doesn't kill the whole script -- we want to process every blob we can
# and report which ones failed at the end.
$Stamp = Get-Date -Format 'yyyyMMddHHmmss'
Write-Host "==> Step 1: bumping lastModified on $($FailedPdfs.Count) blobs (stamp=$Stamp)"
$updated = @()
$skipped = @()
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
foreach ($pdf in $FailedPdfs) {
  Write-Host "  - $pdf" -NoNewline
  $null = az storage blob metadata update `
    --account-name $StorageAcct `
    --container-name $Container `
    --name $pdf `
    --metadata "force_reindex=$Stamp" `
    --auth-mode login 2>&1
  if ($LASTEXITCODE -eq 0) {
    Write-Host "  -> updated" -ForegroundColor Green
    $updated += $pdf
  } else {
    Write-Host "  -> FAILED (blob may not exist or you lack permission)" -ForegroundColor Yellow
    $skipped += $pdf
  }
}
$ErrorActionPreference = $prevEAP

if ($skipped.Count -gt 0) {
  Write-Host ""
  Write-Host "WARN: skipped $($skipped.Count) blob(s) -- continuing with the $($updated.Count) that updated:" -ForegroundColor Yellow
  $skipped | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
}

if ($updated.Count -eq 0) {
  Write-Host ""
  Write-Host "ERROR: no blobs were updated. Aborting before resetdocs/run." -ForegroundColor Red
  exit 1
}

# Step 2: Reset the failed items state for these specific docs.
# Only target blobs we actually managed to update.
Write-Host ""
Write-Host "==> Step 2: clearing failed items list for the $($updated.Count) updated docs"
$BlobUrls = $updated | ForEach-Object {
  "https://${StorageAcct}.${StorageEndpoint}/${Container}/${_}"
}

$Token = az account get-access-token --resource https://search.windows.net --query accessToken -o tsv
if (-not $Token) { throw "Failed to acquire AAD token for Search service" }

$ResetUrl = "$SearchEp/indexers/$IndexerName/resetdocs?api-version=2024-05-01-preview"
$Body = @{ datasourceDocumentIds = $BlobUrls } | ConvertTo-Json -Depth 3

try {
  Invoke-RestMethod -Uri $ResetUrl -Method POST `
    -Headers @{ "Authorization" = "Bearer $Token"; "Content-Type" = "application/json" } `
    -Body $Body | Out-Null
  Write-Host "  -> reset accepted" -ForegroundColor Green
} catch {
  Write-Host "  -> reset failed: $($_.Exception.Message)" -ForegroundColor Yellow
  Write-Host "     Continuing anyway -- metadata bump alone usually works"
}

# Step 3: Trigger indexer run immediately
Write-Host ""
Write-Host "==> Step 3: triggering indexer run"
$RunUrl = "$SearchEp/indexers/$IndexerName/run?api-version=2024-05-01-preview"
Invoke-RestMethod -Uri $RunUrl -Method POST `
  -Headers @{ "Authorization" = "Bearer $Token"; "Content-Length" = "0" } | Out-Null
Write-Host "  -> run triggered" -ForegroundColor Green

Write-Host ""
Write-Host "Indexer will now see these 14 blobs as newly-modified and re-process them."
Write-Host "Each large PDF (50-95 MB) takes 5-15 min through the full pipeline."
Write-Host "Expected: all 14 done in 1-3 hours."
Write-Host ""
Write-Host "Monitor with:"
Write-Host "  python scripts/check_index.py --config $Config --coverage"
