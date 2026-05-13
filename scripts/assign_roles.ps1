# One-shot role assignments for a fresh environment.
#
# Run AFTER:
#   - Bicep / portal provisioning created the resources
#   - The Function App and Search service have system-assigned MI enabled
#   - You ran scripts/deploy_function.ps1 (so the Function App exists)
#
# Run BEFORE:
#   - scripts/deploy_search.py
#   - scripts/preanalyze.py
#
# Usage:
#   1. Fill in the seven names at the top of this file.
#   2. .\scripts\assign_roles.ps1
#   3. Wait 10 minutes for RBAC propagation.
#
# Idempotent: re-running is safe. Roles already assigned are skipped.

$ErrorActionPreference = 'Stop'

# ---------- FILL THESE IN ----------
$Rg      = '<your-rg>'
$Search  = '<search-service-name>'
$Storage = '<storage-account-name>'
$Aoai    = '<aoai-resource-name>'

# $Di and $AiSvc: if you have a *standalone* Document Intelligence resource
# (kind=FormRecognizer) AND a separate Azure AI multi-service account
# (kind=CognitiveServices), set them to those two distinct names.
#
# If you have only ONE multi-service Cognitive Services account that
# bundles DI together with everything else (common in commercial Azure and Gov
# Cloud), set $Di and $AiSvc to the SAME name. The script will assign
# both roles to the same resource — Azure handles that cleanly.
$Di      = '<di-or-multi-service-account-name>'
$AiSvc   = '<ai-services-multi-service-account-name>'

$Func    = '<function-app-name>'
# -----------------------------------

foreach ($pair in @(
    @{ Name = 'Rg';      Val = $Rg      },
    @{ Name = 'Search';  Val = $Search  },
    @{ Name = 'Storage'; Val = $Storage },
    @{ Name = 'Aoai';    Val = $Aoai    },
    @{ Name = 'Di';      Val = $Di      },
    @{ Name = 'AiSvc';   Val = $AiSvc   },
    @{ Name = 'Func';    Val = $Func    }
)) {
    if ($pair.Val -like '<*>') {
        throw "$($pair.Name) is still the placeholder '$($pair.Val)'. Edit the top of this script."
    }
}

Write-Host 'Looking up principal IDs and resource IDs...'
$me        = az ad signed-in-user show --query id -o tsv
$searchId  = az search service show -n $Search -g $Rg --query id -o tsv
$storageId = az storage account show -n $Storage -g $Rg --query id -o tsv
$aoaiId    = az cognitiveservices account show -n $Aoai -g $Rg --query id -o tsv
$diId      = az cognitiveservices account show -n $Di -g $Rg --query id -o tsv
$aisvcId   = az cognitiveservices account show -n $AiSvc -g $Rg --query id -o tsv
$searchMi  = az search service show -n $Search -g $Rg --query 'identity.principalId' -o tsv
$funcMi    = az functionapp identity show -n $Func -g $Rg --query principalId -o tsv

if (-not $searchMi -or -not $funcMi) {
    Write-Error @"
Search or Function App is missing a system-assigned identity. Enable it first, then re-run:
  az functionapp identity assign -n $Func -g $Rg
  az search service update      -n $Search -g $Rg --identity-type SystemAssigned
"@
    exit 1
}

function Grant {
    param([string]$Who, [string]$Role, [string]$Scope)
    Write-Host "  -> $Role"
    az role assignment create --assignee $Who --role $Role --scope $Scope --only-show-errors | Out-Null
}

Write-Host ''
Write-Host 'A. Granting your user (deploying principal) roles...'
Grant $me       'Search Service Contributor'     $searchId
Grant $me       'Search Index Data Contributor'  $searchId
Grant $me       'Storage Blob Data Contributor'  $storageId
Grant $me       'Cognitive Services OpenAI User' $aoaiId
Grant $me       'Cognitive Services User'        $diId

Write-Host ''
Write-Host 'B. Granting Search service MI roles...'
Grant $searchMi 'Storage Blob Data Reader'       $storageId
Grant $searchMi 'Cognitive Services OpenAI User' $aoaiId
Grant $searchMi 'Cognitive Services User'        $aisvcId

Write-Host ''
Write-Host 'C. Granting Function App MI roles...'
Grant $funcMi   'Storage Blob Data Reader'       $storageId
Grant $funcMi   'Cognitive Services OpenAI User' $aoaiId
Grant $funcMi   'Cognitive Services User'        $diId
Grant $funcMi   'Search Index Data Reader'       $searchId

Write-Host ''
Write-Host 'All 12 role assignments submitted.'
Write-Host 'Wait 10 minutes for RBAC propagation before running deploy_search.py.'
