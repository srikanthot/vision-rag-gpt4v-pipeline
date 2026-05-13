# RUNBOOK — MANGOS technical-manuals indexing pipeline

Copy-paste runbook for the full lifecycle: from-scratch clone, full deploy,
single-PDF test run, and the most common recovery paths.

All commands are **PowerShell on Windows**. Bash users can substitute the
obvious equivalents (`Activate.ps1` → `activate`, `\` → `/`, etc.).

---

## TABLE OF CONTENTS

1. [One-time machine setup](#1-one-time-machine-setup)
2. [Clone the repo + Python environment](#2-clone-the-repo--python-environment)
3. [Create deploy.config.json](#3-create-deployconfigjson)
4. [Local sanity check (no cloud cost)](#4-local-sanity-check-no-cloud-cost)
5. [Bootstrap Azure RBAC + permissions](#5-bootstrap-azure-rbac--permissions)
6. [Upload a test PDF](#6-upload-a-test-pdf)
7. [Preanalyze (DI + crops + vision + output assembly)](#7-preanalyze)
8. [Deploy Function App + Search artifacts](#8-deploy-function-app--search-artifacts)
9. [Reset + run indexer](#9-reset--run-indexer)
10. [Validate end-to-end](#10-validate-end-to-end)
11. [RECOVERY: When the indexer hits "DefaultAzureCredential failed"](#11-recovery-defaultazurecredential-failed)
12. [RECOVERY: When the smoke test fails on a specific field](#12-recovery-smoke-test-field-failure)
13. [RECOVERY: Stale rows after a reindex](#13-recovery-stale-rows-after-reindex)
14. [Troubleshooting cheat sheet](#14-troubleshooting-cheat-sheet)

---

## 1. One-time machine setup

Skip if already done on this laptop.

```powershell
# Verify Python 3.11+ installed
python --version

# Verify Azure CLI installed
az --version

# Set Azure to commercial cloud (MANGOS)
az cloud set --name AzureCloud

# Log in
az login

# (If behind Forcepoint corporate proxy) — set SSL trust env vars
$env:SSL_CERT_FILE = "C:\path\to\corp-ca-bundle.crt"
$env:REQUESTS_CA_BUNDLE = "C:\path\to\corp-ca-bundle.crt"
```

---

## 2. Clone the repo + Python environment

```powershell
# Pick a working folder
cd C:\

# Clone fresh
git clone https://github.com/srikanthot/azureindex.git mangosindexv01
cd mangosindexv01

# Create + activate virtual env
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 3. Create deploy.config.json

```powershell
# Copy from the example
Copy-Item deploy.config.example.json deploy.config.json
notepad deploy.config.json
```

Fill in your own values:

| Field | Example |
|---|---|
| `functionApp.name` | `azureindex-functionv03` |
| `functionApp.resourceGroup` | `rg-mangos-tman-dev01` |
| `search.endpoint` | `https://srch02-mangos-tman-dev01.search.windows.net` |
| `search.artifactPrefix` | `techmanuals-v05` |
| `azureOpenAI.endpoint` | `https://mangos-openai.openai.azure.com/` |
| `azureOpenAI.embedDeployment` / `chatDeployment` / `visionDeployment` | your AOAI deployment names |
| `aiServices.endpoint` | `https://aiservicesforocr.cognitiveservices.azure.com/` |
| `storage.accountResourceId` | full ARM ID `/subscriptions/.../samangostmandevv01` |
| `storage.pdfContainerName` | `techmanualsv04` |
| `cosmos.endpoint` / `database` / containers | optional, recommended |

Save and close.

---

## 4. Local sanity check (no cloud cost)

```powershell
python scripts/smoke_test.py --local
python tests/test_unit.py
```

Both must say **PASSED**. If either fails, stop and post the output.

---

## 5. Bootstrap Azure RBAC + permissions

This assigns the Function App's Managed Identity the right roles on
Storage / Search / AOAI / DI / Cosmos. **Run this every time you create
a new Function App or recreate one** — the MI's principalId changes and
old role assignments don't carry over.

### 5a. (First time only) Set up Cosmos DB

If `bootstrap.py` fails preflight with:
```
[FAIL] Cosmos DB cannot reach cosmos database 'indexing' in <your-cosmos>
```

It means EITHER (a) your config still has the placeholder `<your-cosmos>`,
OR (b) the database doesn't exist yet. Follow the numbered steps below
in order. Run ONE command at a time. Don't skip steps.

---

#### Step 1 — List every Cosmos account in your subscription

```powershell
az cosmosdb list --query "[].{name:name, rg:resourceGroup, endpoint:documentEndpoint}" -o table
```

**What to expect**: a table showing your existing Cosmos accounts. Copy
the `endpoint` value of the one you want to use.

If the table is **empty** (no Cosmos accounts at all), skip to Step 1b.
Otherwise skip to Step 2.

---

#### Step 1b — Only if no Cosmos account exists: create one

Skip this step if Step 1 showed an existing account.

```powershell
$RG = (Get-Content deploy.config.json | ConvertFrom-Json).functionApp.resourceGroup
$ACCT = "mangos-cosmos"
$LOC = "usgovvirginia"
az cosmosdb create -g $RG -n $ACCT --locations regionName=$LOC --capabilities EnableServerless
```

**What to expect**: takes ~5 minutes. Output is JSON describing the new
account. Note the `documentEndpoint` field — that's what you'll paste
into the config in Step 2.

---

#### Step 2 — Open deploy.config.json in Notepad

```powershell
notepad deploy.config.json
```

**What to do**: find the `"cosmos"` section. Replace the placeholder
endpoint with your real one from Step 1 (or Step 1b).

Find this:
```json
"cosmos": {
  "endpoint": "https://<your-cosmos>.documents.azure.com:443/",
  "database": "indexing"
}
```

Change to (paste YOUR real endpoint):
```json
"cosmos": {
  "endpoint": "https://mangos-cosmos.documents.azure.com:443/",
  "database": "indexing"
}
```

**Save the file (Ctrl+S). Close Notepad.**

---

#### Step 3 — Verify the config is now correct

```powershell
$CFG = Get-Content deploy.config.json | ConvertFrom-Json
Write-Host "Endpoint in config: $($CFG.cosmos.endpoint)"
```

**What to expect**: the line should show your REAL endpoint (no angle
brackets). If you still see `<your-cosmos>`, Notepad didn't save —
re-do Step 2.

---

#### Step 4 — Find the resource group containing the Cosmos account

```powershell
$COSMOS_ACCOUNT = ($CFG.cosmos.endpoint -replace 'https://', '' -split '\.')[0]
$ACCOUNT_RG = (az cosmosdb list --query "[?name=='$COSMOS_ACCOUNT'].resourceGroup" -o tsv)
Write-Host "Cosmos account: $COSMOS_ACCOUNT"
Write-Host "Resource group: $ACCOUNT_RG"
```

**What to expect**: both lines show real values, not blanks.

If `Resource group:` is empty:
1. Look back at Step 1's output and find the **Rg** column for your
   Cosmos account.
2. Set it manually: `$ACCOUNT_RG = "rg-mangos-tman-dev01"`  (replace with
   the actual RG from your Step 1 table).
3. Re-run the two `Write-Host` lines above to confirm.

---

#### Step 5 — Determine if your Cosmos account is serverless or provisioned

Serverless accounts can't take a `--throughput` flag on database creation.
Check first:

```powershell
$IS_SERVERLESS = (az cosmosdb show --name $COSMOS_ACCOUNT --resource-group $ACCOUNT_RG --query "capabilities[?name=='EnableServerless'] | length(@)" -o tsv)
Write-Host "Serverless? $IS_SERVERLESS  (1 = yes, 0 = no)"
```

---

#### Step 5b — Create the database (serverless path)

If Step 5 said `Serverless? 1`, run this (NO --throughput flag):

```powershell
az cosmosdb sql database create --account-name $COSMOS_ACCOUNT --resource-group $ACCOUNT_RG --name $CFG.cosmos.database
```

If you get `(BadRequest) Shared throughput database creation is not
supported for serverless accounts`, you accidentally included
`--throughput` — drop it and re-run.

---

#### Step 5c — Create the database (provisioned path)

If Step 5 said `Serverless? 0`, run this with throughput:

```powershell
az cosmosdb sql database create --account-name $COSMOS_ACCOUNT --resource-group $ACCOUNT_RG --name $CFG.cosmos.database --throughput 400
```

---

**What to expect from either 5b or 5c**: JSON output describing the
created database, OR a `(Conflict) ... already exists` error (also
fine — means the database is already there).

If you get `(--resource-group | -g) are required`, your `$ACCOUNT_RG`
variable is empty — go back to Step 4 and set it manually.

If you get `(ResourceNotFound)`, the account isn't in the RG you
specified — re-check Step 1's table for the correct `Rg` column value.

---

#### Step 6 — Re-run bootstrap

```powershell
python scripts/bootstrap.py --config deploy.config.json --auto-fix
```

**What to expect**: STEP 1/8 (preflight) should now show `[OK] Cosmos DB`
instead of `[FAIL]`. The script proceeds through STEPs 2–8 and finishes
with role assignments. The `[FAIL] LibreOffice (optional)` line is
expected and safe to ignore.

If you see `Bootstrap complete.` at the end, you're done with this
section. Continue to **Section 5c** (the 10-minute wait).

### 5b. Run bootstrap

```powershell
python scripts/bootstrap.py --config deploy.config.json --auto-fix
```

You should see all 8 STEPs pass and end with something like:
```
[OK]   Cosmos DB                cdb-ragchat-dev / indexing accessible
[WARN] LibreOffice (optional)   not on PATH (optional, OK to ignore for PDF-only corpora)

1 advisory warning(s) — review but not blocking:
  * LibreOffice (optional): not on PATH (optional, OK to ignore for PDF-only corpora)

All required checks passed. You can run preanalyze now.

STEP 2/8 — ...
...
STEP 8/8 — Verify Function App MI roles
[OK] Storage Blob Data Reader assigned
[OK] Search Index Data Contributor assigned
[OK] Cognitive Services User on AOAI assigned
[OK] Cognitive Services User on AI Services assigned
Bootstrap complete.
```

The LibreOffice line shows as `[WARN]` not `[FAIL]` — it's an advisory
notice and does NOT block bootstrap. Only matters if you index
.docx/.pptx/.xlsx files. For PDF-only corpora, ignore.

If a non-LibreOffice check fails, fix what it reports and re-run.

### 5c. Wait 10 minutes for RBAC propagation

Don't skip this. Triggering anything before propagation gives you the
SAME `DefaultAzureCredential failed` error and you'll think the fix
didn't work.

```powershell
Write-Host "Waiting 10 min for RBAC propagation..."
Start-Sleep -Seconds 600
Write-Host "Done waiting."
```

---

## 6. Upload a test PDF

Skip if your PDF is already in the container.

```powershell
$STORAGE_ACCOUNT = "samangostmandevv01"
$CONTAINER       = "techmanualsv04"
$LOCAL_PATH      = "C:\path\to\ED-ED-ATD.pdf"
$BLOB_NAME       = "ED-ED-ATD.pdf"

az storage blob upload `
  --account-name $STORAGE_ACCOUNT `
  --container-name $CONTAINER `
  --name $BLOB_NAME `
  --file $LOCAL_PATH `
  --auth-mode login
```

---

## 7. Preanalyze

Runs DI + figure cropping + GPT-4 vision + output assembly. Takes 5–15 min
depending on PDF size and figure count.

```powershell
# Full run (DI + vision + output)
python scripts/preanalyze.py --config deploy.config.json
```

If you already have valid DI + vision cache (just need to refresh
output.json after a code change):

```powershell
python scripts/preanalyze.py --config deploy.config.json --phase output --force
```

---

## 8. Deploy Function App + Search artifacts

```powershell
# Deploy the Function App code
.\scripts\deploy_function.ps1

# Wait for it to start
Start-Sleep -Seconds 60

# Push the search index, skillset, datasource, indexer
python scripts/deploy_search.py --config deploy.config.json
```

Expected output from `deploy_search.py`:

```
ok  datasources/<prefix>-ds (201 or 204)
ok  indexes/<prefix>-index (201 or 204)
ok  skillsets/<prefix>-skillset (201 or 204)
ok  indexers/<prefix>-indexer (201 or 204)
```

---

## 9. Reset + run indexer

```powershell
.\scripts\reset_indexer.ps1
```

Wait ~60 seconds for the indexer to complete (the heavy work was already
done in preanalyze).

```powershell
Start-Sleep -Seconds 60
```

---

## 10. Validate end-to-end

```powershell
python scripts/smoke_test.py --config deploy.config.json --skip-run
```

Expected ending:

```
text: <N> record(s)
diagram: <N> record(s)
table: <N> record(s)
table_row: <N> record(s)   (or 0 if no 5-80 row tables)
summary: 1 record(s)
SMOKE TEST PASSED
```

---

## 11. RECOVERY: "DefaultAzureCredential failed"

You see this in the indexer execution status:

```
ClientAuthenticationError DefaultAzureCredential failed to retrieve a token
ManagedIdentityCredential authentication unavailable, no response from the IMDS endpoint
```

This means the Function App's Managed Identity has no role assignments on
the resources our code calls. Almost always happens after creating a new
Function App.

```powershell
# 1. Read config
$CFG = Get-Content deploy.config.json | ConvertFrom-Json
$RG  = $CFG.functionApp.resourceGroup
$FN  = $CFG.functionApp.name
Write-Host "Function App: $FN / Resource Group: $RG"

# 2. Verify MI is enabled, get principalId
$PRINCIPAL = (az functionapp identity show -g $RG -n $FN --query principalId -o tsv)
Write-Host "Principal ID: $PRINCIPAL"

if (-not $PRINCIPAL) {
    az functionapp identity assign -g $RG -n $FN
    Start-Sleep -Seconds 5
    $PRINCIPAL = (az functionapp identity show -g $RG -n $FN --query principalId -o tsv)
    Write-Host "Enabled MI. New principalId: $PRINCIPAL"
}

# 3. Assign roles
python scripts/bootstrap.py --config deploy.config.json --auto-fix

# 4. WAIT 10 MINUTES for RBAC propagation (mandatory)
Write-Host "Waiting 10 min for RBAC propagation..."
Start-Sleep -Seconds 600
Write-Host "Done waiting."

# 5. Restart Function App so it re-acquires identity tokens
az functionapp restart -g $RG -n $FN
Start-Sleep -Seconds 30

# 6. Verify the NEW Function App has the latest code
$KUDU_USER = (az webapp deployment list-publishing-credentials -g $RG -n $FN --query publishingUserName -o tsv)
$KUDU_PASS = (az webapp deployment list-publishing-credentials -g $RG -n $FN --query publishingPassword -o tsv)
$AUTH = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${KUDU_USER}:${KUDU_PASS}"))
$URL  = "https://$FN.scm.azurewebsites.net/api/vfs/site/wwwroot/shared/skill_io.py"

try {
    $resp = Invoke-WebRequest -Uri $URL -Headers @{Authorization="Basic $AUTH"} -UseBasicParsing
    if ($resp.Content -match '"data": None') {
        Write-Host "OK Function App has the latest skill_io.py"
        $needs_redeploy = $false
    } else {
        Write-Host "STALE - skill_io.py is OLD"
        $needs_redeploy = $true
    }
} catch {
    Write-Host "Could not fetch from Kudu: $_"
    $needs_redeploy = $true
}

# 7. Redeploy if stale
if ($needs_redeploy) {
    .\scripts\deploy_function.ps1
    Start-Sleep -Seconds 30
    az functionapp restart -g $RG -n $FN
    Start-Sleep -Seconds 30
}

# 8. Reset + run indexer
.\scripts\reset_indexer.ps1

# 9. Validate
Start-Sleep -Seconds 60
python scripts/smoke_test.py --config deploy.config.json --skip-run
```

---

## 12. RECOVERY: smoke test field failure

The smoke test prints the exact field that didn't pass its contract:

```
text.callouts: empty list
diagram.figure_bbox: expected JSON array, got dict
```

Most common causes:

| Failure | Meaning | Fix |
|---|---|---|
| `text.callouts: empty list` | No WARNING/DANGER/CAUTION in chunk OR extractor regressed | Verify by running `_extract_callouts()` in tests |
| `diagram.figure_bbox: expected JSON array, got dict` | Function App is running pre-Sprint-2 code | Redeploy: `.\scripts\deploy_function.ps1` |
| `*.physical_pdf_pages missing start=N` | bbox cross-validation regression | Pull latest, redeploy |
| `figures_referenced_normalized: empty list` | Function App pre-Sprint-2 OR text has no Figure refs | Verify by chunk content; redeploy if needed |

---

## 13. RECOVERY: stale rows after reindex

After re-indexing with new code, old rows from previous skill_versions
hang around as near-duplicates. Reap them:

```powershell
# Dry-run first (safe — won't delete)
python scripts/reap_stale_rows.py --config deploy.config.json --dry-run

# Actually delete
python scripts/reap_stale_rows.py --config deploy.config.json --yes
```

---

## 14. Troubleshooting cheat sheet

| Symptom | Most likely cause | Fix |
|---|---|---|
| `pip install` SSL error | Forcepoke env vars not set | Section 1, set SSL_CERT_FILE |
| `bootstrap.py` "AuthorizationFailed" | Need Owner/User Access Admin on RG | Have an admin run it |
| `deploy_search.py` 400 on skillset | Schema attribute change rejected | Use a fresh `artifactPrefix` |
| Indexer "Web Api response contains both data and errors" | Function App running pre-`228762f` code | Redeploy: `.\scripts\deploy_function.ps1` |
| Indexer "DefaultAzureCredential failed" | New MI without roles | Section 11 |
| Indexer "did not execute within 00:01:00" | Skill timeout (was PT60S) | Pull latest (PT230S now), redeploy |
| Indexer "Missing or empty value '/.../pdf_total_pages'" | Old output.json missing per-item field | `preanalyze.py --phase output --force` |
| Function App 500 errors with no detail | Cold-start import crash | Pull latest (lazy openai, Pillow declared), redeploy |
| Function App returns null vectors | ConditionalSkill misbehaving | Pull latest (`1f0df2a` reverted them) |
| Cosmos `run_history upsert failed` locally | Local CLI lacks Cosmos data role | Non-fatal, ignore — preanalyze still completes |

---

## Quick sanity checks anytime

```powershell
# Local schema-consistency (free)
python scripts/smoke_test.py --local

# Unit tests (free)
python tests/test_unit.py

# Index status: how many records of each type
python scripts/check_index.py --config deploy.config.json

# Reap stale rows
python scripts/reap_stale_rows.py --config deploy.config.json --dry-run
```
