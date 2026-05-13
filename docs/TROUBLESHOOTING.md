# Troubleshooting — copy-paste diagnostic commands

When deploy_search.py / preanalyze.py / run_pipeline.py fails on a fresh
laptop or environment, walk down this page and run the matching checks.
Every command is copy-paste ready for **PowerShell on Windows**.
Equivalent bash for Linux/Mac is included where it differs.

> **First: always set your SSL cert env vars** if your environment uses
> a corporate proxy / TLS inspection (Forcepoint, Zscaler, etc.):
>
> ```powershell
> $env:SSL_CERT_FILE = "C:\Users\<you>\Downloads\combined-ca.crt"
> $env:REQUESTS_CA_BUNDLE = "C:\Users\<you>\Downloads\combined-ca.crt"
> ```
>
> If you don't have a combined-ca.crt, your IT/security team can give
> you one. Without it, every Python HTTPS call fails with
> `SSL: CERTIFICATE_VERIFY_FAILED`.

---

## 0. Quick decision tree

| Symptom | Jump to |
|---|---|
| `SSL: CERTIFICATE_VERIFY_FAILED` | [§1](#1-ssl-cert-error) |
| `PUT datasources/... failed: 403` | [§2](#2-deploy_searchpy-403) |
| `Forbidden by IP firewall` in 403 body | [§3](#3-search-service-firewall) |
| HTML body in 403 mentioning Forcepoint / corp proxy | [§4](#4-corporate-proxy-blocking) |
| `not authorized to perform action` in 403 body | [§5](#5-rbac--identity-issues) |
| `assign_roles.py` says "already assigned" but deploy still 403 | [§5](#5-rbac--identity-issues) |
| Works in one resource group but not another | [§6](#6-subscription--tenant-mismatch) |
| `LibreOffice not found` warning | [§7](#7-libreoffice-missing) |
| `blob HEAD unexpected 403` on PDFs with spaces | already fixed in code; pull latest |
| Indexer stuck "in progress" for hours | [§8](#8-indexer-stuck) |
| `cosmos run_history upsert failed` | [§9](#9-cosmos-db-issues) |

---

## 1. SSL cert error

**Symptom:**
```
httpx.ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self signed certificate in certificate chain
```

**Cause:** corporate TLS inspection rewriting cert chain.

**Fix (PowerShell):**
```powershell
$env:SSL_CERT_FILE = "C:\Users\<you>\Downloads\combined-ca.crt"
$env:REQUESTS_CA_BUNDLE = "C:\Users\<you>\Downloads\combined-ca.crt"
```

**Fix (bash):**
```bash
export SSL_CERT_FILE="$HOME/Downloads/combined-ca.crt"
export REQUESTS_CA_BUNDLE="$HOME/Downloads/combined-ca.crt"
```

These env vars only live in your current shell session. Re-set them
every time you open a new terminal, or add them to your PowerShell
profile / `~/.bashrc`.

---

## 2. deploy_search.py 403

When you see:

```
PUTting artifacts to https://<search>.search.windows.net
PUT datasources/<name>-ds failed: 403
```

The script truncates the response body. Get the **actual 403 message** —
this is the single most useful diagnostic:

```powershell
python -c @"
import httpx, json, base64
from azure.identity import DefaultAzureCredential

cfg = json.loads(open('deploy.config.json').read())
endpoint = cfg['search']['endpoint'].rstrip('/')

cred = DefaultAzureCredential()
token_obj = cred.get_token('https://search.windows.net/.default')

# Decode the JWT to confirm which identity Python is actually using
parts = token_obj.token.split('.')
pad = '=' * ((4 - len(parts[1]) % 4) % 4)
claims = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
print('Token issued for:')
print('  upn:', claims.get('upn') or claims.get('appid'))
print('  oid:', claims.get('oid'))
print('  tenant:', claims.get('tid'))
print('  audience:', claims.get('aud'))
print()

r = httpx.get(f'{endpoint}/datasources?api-version=2024-11-01-preview',
              headers={'Authorization': 'Bearer ' + token_obj.token}, timeout=30)
print(f'GET status: {r.status_code}')
print(f'BODY (first 1500 chars):')
print(r.text[:1500])
"@
```

Read the body and match against:

| Body says | Means | Fix |
|---|---|---|
| `Forbidden by IP firewall` | Search service blocks your IP | [§3](#3-search-service-firewall) |
| HTML page with `Forcepoint` or company proxy branding | Corporate proxy blocking outbound | [§4](#4-corporate-proxy-blocking) |
| `not authorized to perform action` / `AuthorizationFailed` | RBAC role missing or wrong identity | [§5](#5-rbac--identity-issues) |
| `PrincipalNotFound` | RBAC propagation incomplete | wait 10–30 min, retry |
| `apiKeyOnly` / `requires admin key` | Search service rejects AAD | [§5.4](#54-search-service-in-apikeyonly-mode) |

---

## 3. Search service firewall

Verify network rules:

```powershell
$cfg = Get-Content deploy.config.json | ConvertFrom-Json
$searchName = ($cfg.search.endpoint -replace 'https://','').Split('.')[0]
$rg = $cfg.functionApp.resourceGroup

az search service show -n $searchName -g $rg `
  --query "{publicAccess:publicNetworkAccess, ipRules:networkRuleSet.ipRules}" -o json
```

If `publicAccess` is `"disabled"` or `ipRules` is restrictive:

**Add your IP to the allowlist:**
```powershell
$myIp = (Invoke-RestMethod -Uri "https://api.ipify.org")
Write-Host "Your public IP: $myIp"

# If api.ipify.org returns HTML (proxy intercepting), use this instead:
$myIp = (curl.exe -s https://ifconfig.me/ip).Trim()
Write-Host "Your public IP: $myIp"

# Or check Windows network adapter
Get-NetIPAddress | Where-Object {$_.AddressFamily -eq "IPv4" -and $_.PrefixOrigin -eq "Dhcp"} | Select-Object IPAddress

az search service update -n $searchName -g $rg --ip-rules $myIp
```

**Or enable public access entirely:**
```powershell
az search service update -n $searchName -g $rg --public-network-access enabled
```

---

## 4. Corporate proxy blocking

**Symptom:** the 403 body contains HTML like:
```html
<title>Access to this site is blocked</title>
Copyright (c) 2022 Forcepoint
... credential prompt form ...
```

**Cause:** your corporate web filter (Forcepoint, Zscaler, Bluecoat, etc.)
is intercepting outbound HTTPS to `*.azure.com` and returning its own
block page. The traffic never reaches Azure.

**Verify:** open the search URL in your browser:
```
https://<search>.search.windows.net
```
- Real Azure: TLS cert subject is `*.search.windows.net`, issuer is
  Microsoft-something. Page returns Azure's own 403 (no UI).
- Proxy intercept: TLS cert subject is your company / Forcepoint.
  Page is the proxy's branded "blocked" page.

```powershell
# Check the certificate chain
curl.exe -v https://<search>.search.windows.net 2>&1 | Select-String -Pattern "issuer|subject"
```

**Fixes:**
1. **Get Azure Gov endpoints allowlisted** — submit IT ticket asking for:
   ```
   *.search.windows.net
   *.openai.azure.com
   *.cognitiveservices.azure.com
   *.documents.azure.com
   *.blob.core.windows.net
   *.azurewebsites.net
   login.microsoftonline.com
   ```
2. **Use Forcepoint credential override** if your team has elevated
   credentials for proxy bypass.
3. **Run from Jenkins** — the CI agent is inside the corporate network
   with proper allowlisting. Push to main, let `Jenkinsfile.deploy`
   run there instead of from your laptop.

---

## 5. RBAC / identity issues

### 5.1 Verify which identity Python is using

The diagnostic in [§2](#2-deploy_searchpy-403) prints the JWT claims.
Confirm:
- `upn` matches your login email
- `tid` (tenant) is your expected tenant
- `aud` (audience) is `https://search.windows.net`

If the identity is wrong, force a refresh:

```powershell
az logout
az login
az account set --subscription "<the-sub-where-resources-live>"
```

### 5.2 Verify the role IS on THIS search service

```powershell
$cfg = Get-Content deploy.config.json | ConvertFrom-Json
$searchName = ($cfg.search.endpoint -replace 'https://','').Split('.')[0]
$rg = $cfg.functionApp.resourceGroup

$me = az ad signed-in-user show --query id -o tsv
$searchId = az search service show -n $searchName -g $rg --query id -o tsv

Write-Host "Looking for roles for principal $me on $searchId"
az role assignment list --assignee $me --scope $searchId `
  --query "[].{role:roleDefinitionName, scope:scope}" -o table
```

You should see at least:
```
Search Service Contributor
Search Index Data Contributor
```

If empty → re-run `assign_roles.py` after confirming the correct
subscription is selected (see [§6](#6-subscription--tenant-mismatch)).

### 5.3 Verify Function App MI has its roles

```powershell
$funcMi = az functionapp identity show -n $cfg.functionApp.name -g $rg --query principalId -o tsv
$storageId = $cfg.storage.accountResourceId
$searchId = az search service show -n $searchName -g $rg --query id -o tsv

az role assignment list --assignee $funcMi --query "[].{role:roleDefinitionName, scope:scope}" -o table
```

Function App MI should have:
- Storage Blob Data Reader on storage account
- Cognitive Services OpenAI User on AOAI
- Cognitive Services User on Document Intelligence
- Search Index Data Reader on Search service

### 5.4 Search service in apiKeyOnly mode

```powershell
az search service show -n $searchName -g $rg `
  --query "{authOptions:authOptions, disableLocalAuth:disableLocalAuth}" -o json
```

If `authOptions` is `apiKeyOnly` or AAD is disabled, AAD tokens get 403.

```powershell
# Re-enable AAD (or aadOrApiKey)
az search service update -n $searchName -g $rg `
  --aad-auth-failure-mode http403 --auth-options aadOrApiKey
```

Wait 5 minutes after this change.

---

## 6. Subscription / tenant mismatch

Most common when "it works in another RG but not this one".

```powershell
# What sub am I using right now?
az account show --query "{name:name, id:id, tenant:tenantId}" -o table

# What sub does the resource live in?
$cfg = Get-Content deploy.config.json | ConvertFrom-Json
$searchName = ($cfg.search.endpoint -replace 'https://','').Split('.')[0]
az resource list --resource-type "Microsoft.Search/searchServices" --name $searchName `
  --query "[].{name:name, id:id}" -o table
```

If the sub IDs don't match → switch and re-grant:

```powershell
# Switch to the correct sub
az account set --subscription "<sub-id-from-the-resource>"

# Re-run RBAC bootstrap (idempotent)
python scripts/assign_roles.py --config deploy.config.json --wait-for-propagation 300

# Force fresh token in the new sub context
az logout
az login
az account set --subscription "<sub-id-from-the-resource>"

# Retry deploy
python scripts/deploy_search.py --config deploy.config.json
```

Also verify your tenant is correct:

```powershell
# List all tenants you can access
az account list --query "[].{name:name, tenantId:tenantId, sub:id}" -o table
```

Sometimes guest accounts pull the wrong tenant by default.

---

## 7. LibreOffice missing

**Symptom:** preanalyze logs:
```
warn: skipping conversion for X.pptx -- LibreOffice not installed.
Indexing text + tables only.
```

This is **not an error**, just a warning. PDFs are unaffected. PPTX/DOCX/XLSX
get text + tables but no figure extraction.

**To enable figure extraction on non-PDF files:**

```powershell
# Windows: download from libreoffice.org, install with default options
# Then verify
soffice --version
```

```bash
# Linux
sudo apt-get install -y libreoffice  # Ubuntu/Debian
sudo dnf install -y libreoffice      # RHEL/Fedora

# macOS
brew install --cask libreoffice
```

After install, restart your terminal and re-run preflight:
```powershell
python scripts/preflight.py --config deploy.config.json
```

---

## 8. Indexer stuck

If the Azure portal shows the indexer "In progress" for >24h:

```powershell
python scripts/check_index.py --config deploy.config.json --check-stuck-indexer
```

Returns:
- exit 0: healthy
- exit 2: stuck (in_progress >24h, OR last 5 runs all failed)
- exit 3: cannot fetch status

If stuck:
```powershell
# Reset clears the high-water mark; next run reprocesses every blob
.\scripts\reset_indexer.ps1   # Windows
./scripts/reset_indexer.sh    # Linux/Mac
```

If indexer hits 230s timeout repeatedly → preanalyze cache is missing for
some PDF. Run:
```powershell
python scripts/preanalyze.py --config deploy.config.json --status
python scripts/preanalyze.py --config deploy.config.json --incremental
```

Full failure-mode catalogue: [SCENARIOS.md](SCENARIOS.md).

---

## 9. Cosmos DB issues

Cosmos is **optional** — pipeline works without it. If `cosmos.endpoint`
or `cosmos.database` is blank in deploy.config.json, all Cosmos writes
silently no-op.

**To enable Cosmos:**

```powershell
# 1. Provision Cosmos account (your architect via Bicep, or):
az cosmosdb create -n <cosmos-name> -g <rg> --kind GlobalDocumentDB `
  --default-consistency-level Session

# 2. Create the database
az cosmosdb sql database create -a <cosmos-name> -g <rg> -n indexing

# 3. Edit deploy.config.json
#    "cosmos": {
#      "endpoint": "https://<cosmos-name>.documents.azure.com:443/",
#      "database": "indexing"
#    }

# 4. Re-run RBAC (idempotent — only grants missing Cosmos roles)
python scripts/assign_roles.py --config deploy.config.json

# 5. Containers auto-create on first write
python scripts/run_pipeline.py --config deploy.config.json
```

**Cosmos write failures don't fail the pipeline.** They log a warning and
move on. Check Jenkins console output for `cosmos ... upsert failed`
warnings if dashboards aren't updating.

---

## 10. First-time setup, full sequence

If you're starting fresh on a new resource group / new laptop:

```powershell
# 1. Set SSL env vars (every new shell)
$env:SSL_CERT_FILE = "C:\Users\<you>\Downloads\combined-ca.crt"
$env:REQUESTS_CA_BUNDLE = "C:\Users\<you>\Downloads\combined-ca.crt"

# 2. Set Azure cloud + login
az cloud set --name AzureCloud
az login
az account set --subscription "<your-sub-id>"

# 3. Verify environment
python scripts/preflight.py --config deploy.config.json

# 4. Grant RBAC roles
python scripts/assign_roles.py --config deploy.config.json --wait-for-propagation 300

# 5. Force fresh token after RBAC change
az logout
az login
az account set --subscription "<your-sub-id>"

# 6. Deploy function app code
.\scripts\deploy_function.ps1 -Config .\deploy.config.json

# 7. Deploy search artifacts (this is where 403 typically appears
#    if RBAC / network / identity is off)
python scripts/deploy_search.py --config deploy.config.json

# 8. Validate
python scripts/smoke_test.py --config deploy.config.json

# 9. Now you can upload PDFs and run the pipeline
python scripts/run_pipeline.py --config deploy.config.json --auto-heal
```

If step 7 fails, run [§2](#2-deploy_searchpy-403) and follow the
matching sub-section.

---

## 11. When all else fails — run from Jenkins

Your Jenkins agent runs **inside the corporate network** with proper
allowlists, in the **correct subscription** (configured by infra), with
**managed identity** that already has the right roles.

If your laptop hits any of: corporate proxy block, IP firewall block,
weird tenant issue → push to main and let Jenkins do it:

1. Commit + push your changes:
   ```powershell
   git add deploy.config.json    # NO — never commit this!
   # Instead, upload deploy.config.json into Jenkins as a secret-file
   # credential (deploy-config-dev / deploy-config-prod). One-time per env.

   git add <your-actual-changes>
   git commit -m "<message>"
   git push origin main
   ```
2. Open Jenkins → trigger `Jenkinsfile.deploy` for the right environment
3. Watch the console output. The pipeline runs:
   - tests + lint
   - manual approval (for prod)
   - assign_roles.py
   - deploy_function.sh
   - deploy_search.py    ← will succeed where laptop fails
   - smoke_test.py

`Jenkinsfile.run` runs the operational pipeline nightly + on demand.

---

## 12. Useful one-liners

```powershell
# What's my current Azure context?
az account show --query "{user:user.name, sub:name, tenant:tenantId}" -o table

# What roles do I have right now (across all scopes)?
$me = az ad signed-in-user show --query id -o tsv
az role assignment list --assignee $me --query "[].{role:roleDefinitionName, scope:scope}" -o table

# Coverage check on the index
python scripts/check_index.py --config deploy.config.json --coverage

# What's stuck in preanalyze?
python scripts/preanalyze.py --config deploy.config.json --status

# Indexer last run status
$cfg = Get-Content deploy.config.json | ConvertFrom-Json
$endpoint = $cfg.search.endpoint
$prefix = $cfg.search.artifactPrefix ?? "mm-manuals"
$token = az account get-access-token --resource "https://search.windows.net" --query accessToken -o tsv
curl.exe -s -H "Authorization: Bearer $token" `
  "$endpoint/indexers/$prefix-indexer/status?api-version=2024-11-01-preview" | python -m json.tool

# Storage container PDF count
az storage blob list --account-name <storage> --container-name <container> `
  --auth-mode login --query "length([?ends_with(name, '.pdf')])" -o tsv

# Trigger the indexer manually
.\scripts\reset_indexer.ps1
```

---

## 13. Pasting an error into chat for help

When asking for help with a 403 or other failure, include:

1. **The exact error message** (run the diagnostic in §2 to get the body)
2. **The JWT claims** (from the same diagnostic) — `upn`, `oid`, `tid`, `aud`
3. **Your Azure context**: `az account show -o table`
4. **The role list**: `az role assignment list --assignee $me --scope $searchId -o table`
5. **The Search service config**: `az search service show -n <name> -g <rg> -o json`

With those five things, the cause is usually obvious within minutes.
