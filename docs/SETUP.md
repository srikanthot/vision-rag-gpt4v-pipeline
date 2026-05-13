# Setup — architecture, provisioning, RBAC, Jenkins, dashboards, index reference

Single document covering everything you need to take the indexing
pipeline from "Bicep just provisioned the resources" to "indexer is
running and the dashboard is showing real data".

If you're an operator who just wants to run the pipeline, jump to
[RUNBOOK.md](RUNBOOK.md). This file is for the person setting it all
up the first time.

## Table of contents

1. [Architecture](#1-architecture) — why the pipeline looks the way it does
2. [Bootstrap](#2-bootstrap) — copy-paste recipe from a fresh environment
3. [RBAC](#3-rbac) — every identity and the roles it needs
4. [Jenkins](#4-jenkins) — agent setup + pipelines
5. [Dashboard spec](#5-dashboard-spec) — Cosmos schema for Power BI
6. [Search index reference](#6-search-index-reference) — schema, queries, semantic config

---


# 1. Architecture


This document explains the design decisions behind the indexing pipeline, the
Azure AI Search skills we use, why we needed a pre-analysis step, what changes
between Azure Commercial and Azure, and how the system behaves for
very large technical manuals.

It is intended for engineers joining the project, for reviewers evaluating the
architecture, and for anyone asking "why not just use the Azure AI Search
wizard?"

---

## 1. What this system does

Given a blob container of PDF technical manuals, it:

1. Extracts text sections, tables, and figures with Document Intelligence.
2. Runs GPT-4 Vision on each figure to produce a retrievable description
   plus OCR of every label, part number, wire tag, and callout.
3. Chunks text with overlap, preserving header chain and page numbers.
4. Generates embeddings (ada-002) for every chunk.
5. Writes everything to an Azure AI Search index with semantic ranker and
   hybrid (vector + BM25) retrieval.

The index powers a RAG experience where a user can ask "what's connected to
terminal X7 on the overcurrent relay?" and get back the exact figure, its
section context, and a citation to the page in the source PDF.

---

## 2. The full pipeline end to end

```
Blob container (PDFs)
        |
        v
+------------------------+
|  preanalyze.py         |   <-- runs OFFLINE, caches to blob
|  (scripts/)            |
+------------------------+
        |
        |  Writes to _dicache/ in same container:
        |   - <pdf>.di.json           (DI result)
        |   - <pdf>.crop.<fig>.json   (per-figure PNG + bbox)
        |   - <pdf>.vision.<fig>.json (per-figure GPT-4V output)
        |   - <pdf>.output.json       (assembled enriched output)
        v
+-----------------------------------------------+
|  Azure AI Search indexer                       |
|  --> runs skillset:                            |
|                                                |
|  1. layout-skill       (built-in DI)           |  text markdown extraction
|  2. split-pages        (built-in SplitSkill)   |  chunk text into pages
|  3. extract-page-label (custom -> page_label.py)| page numbers for text
|  4. process-document   (custom -> reads cache) |  pulls figures/tables
|  5. analyze-diagram    (custom -> reads cache) |  one record per figure
|  6. process-table      (custom -> markdown)    |  one record per table
|  7. embed-*            (4x built-in AOAI)      |  vectors
|                                                |
|  --> writes to Azure AI Search index           |
+-----------------------------------------------+
        |
        v
   Index: techmanuals-v02-index
   Each record is either record_type="text", "diagram", "table", or "summary"
```

---

## 3. Azure AI Search skills in use

The skillset uses a mix of **built-in Microsoft skills** and **custom WebAPI
skills** hosted in our Azure Function App.

### Built-in skills (no code, configured in JSON)

| Skill | Purpose |
|---|---|
| `DocumentIntelligenceLayoutSkill` | Turns PDF into markdown with h1-h3 section headers |
| `SplitSkill` | Chunks markdown into ~1200-char pages with 200-char overlap |
| `AzureOpenAIEmbeddingSkill` (x4) | Calls ada-002 to vectorize every chunk (text, diagram, table, summary) |

### Custom WebAPI skills (our Function App)

| Skill | Purpose | Why custom |
|---|---|---|
| `extract-page-label-skill` | Maps every text chunk to its physical PDF page and printed page label | No built-in skill does this |
| `process-document-skill` | Extracts figures + tables per document; reads pre-analyzed cache | DI results too large/slow for built-in path; need per-figure cropping |
| `analyze-diagram-skill` | Produces one record per figure with GPT-4V description and OCR | No native GPT-4V skill in Azure AI Search |
| `process-table-skill` | Emits one record per table with caption, page range, markdown | Tables need their own record type for retrieval |
| `generate-summary-skill` | Produces a per-document summary record | Optional per-doc overview |

All custom skills run in Python inside the Azure Function App defined under
[function_app/](../function_app/).

---

## 4. Why we pre-analyze (the most important design decision)

Pre-analysis exists because **we hit three hard constraints if we tried to do
everything inside custom skills at index time**:

### Constraint 1: The 230-second skill timeout

Every custom WebAPI skill invocation in Azure AI Search has a hard 230-second
(3 min 50 sec) timeout. This is enforced at the service layer and cannot be
increased. It applies to Azure Commercial and Azure equally.

### Constraint 2: Document Intelligence on big PDFs takes minutes

For a typical 500-page technical manual with figures and tables, a DI layout
call takes **3-15 minutes**. If we run DI inside a custom skill, the skill
times out before DI finishes. The document fails to index.

### Constraint 3: Vision analysis scales with figure count

A typical manual has **500-1,500 figures**. Each figure needs a GPT-4 Vision
call (~1-3 seconds). Total vision time per PDF: **10-75 minutes**. This cannot
fit into a single 230-second skill invocation no matter how parallel we make
it.

### How preanalyze solves this

We run `scripts/preanalyze.py` offline, BEFORE the indexer:

- DI runs with a 15-minute timeout, not 230 seconds
- Vision calls run in parallel (~40 concurrent) across all figures
- Both results are cached to blob storage under `_dicache/`
- At index time, custom skills read from the cache in milliseconds

The indexer never has to wait for DI or vision calls. Skill invocations finish
well under 230 seconds. Huge PDFs index reliably.

### The cost savings angle

Preanalyze also **protects us from paying vision costs twice**. If we
re-index after a schema change or a bug fix, the vision results are already
cached. We don't re-invoke GPT-4V and re-pay for 1,500 calls per PDF.

---

## 5. Cache layout under `_dicache/`

For a PDF named `manual.pdf`, preanalyze creates these blobs:

| Blob | Contents | When written |
|---|---|---|
| `_dicache/manual.pdf.di.json` | Full DI layout result (markdown, figures, tables) | After DI analyze succeeds |
| `_dicache/manual.pdf.crop.<fig_id>.json` | One per figure: base64 PNG + polygon bbox | After cropping each qualifying figure |
| `_dicache/manual.pdf.vision.<fig_id>.json` | One per figure: GPT-4V JSON output (category, description, OCR, figure_ref) | After each vision call succeeds |
| `_dicache/manual.pdf.output.json` | Final assembled enriched figures + tables | After all phases succeed |

`output.json` is the **completion marker**. If present, the PDF is fully
pre-analyzed and the indexer will skip all the expensive work.

Error-caching: if a vision call fails with a transient error, we store an
`_error` record with an `_attempts` count. After 3 retries across runs we
treat it as permanent (e.g. a content-filter block on a nameplate that
triggered a false-positive violence filter) and stop retrying.

---

## 6. Azure Commercial vs Azure

The US Gov Cloud typically lags Commercial by 6-18 months on new features.
Teams often ask "can we skip all this custom code if we move to commercial?"
The honest answer is: **partly yes for simple PDFs, no for technical manuals
at scale**.

### What's the same in both clouds

- The 230-second WebAPI skill timeout
- `DocumentIntelligenceLayoutSkill` (built-in)
- `SplitSkill`, embedding skills
- Document Intelligence API
- Azure OpenAI (though model catalog differs)

### What Commercial has that Gov doesn't yet

| Feature | Commercial | Gov |
|---|---|---|
| Integrated Vectorization wizard | Full support | Partial |
| Multimodal embeddings / vision-aware chunking | Preview/GA | Not available |
| `GenAI Prompt Skill` (built-in GPT-4/4o call) | Preview/GA in some regions | Not available |
| Latest GPT-4o / o-series models | Full catalog | Subset |
| Knowledge Agents / AI Foundry agents | GA | Rolling out |

### Could commercial let us drop preanalyze?

For **simple PDFs** (policy documents, contracts, mostly text, few figures):
Yes. The Import and Vectorize wizard + built-in multimodal skills can
ingest a 50-page contract end to end without any Function App code.

For **technical manuals with 500-2,000 figures each**:
**No.** Even in commercial, the architecture converges to preanalyze for three
reasons:

1. The 230s timeout still applies. Vision across 1,500 figures cannot fit.
2. Built-in multimodal skills in commercial produce **generic image tags**
   ("diagram", "chart", "text"). They do not understand circuit schematics,
   wire tags, terminal labels, or nameplate OCR. For deep technical queries
   ("what is connected to terminal X7 on the overcurrent relay?") generic
   tags fail.
3. Cost. 1,500 vision calls per PDF x every indexer retry x every schema
   iteration. Caching is non-negotiable at scale.

### What would change if we ran on commercial today

- We could replace our custom `analyze-diagram-skill` with `GenAI Prompt Skill`
  to reduce custom-code lines, but still need preanalyze for scale
- Simple non-technical PDFs could go through the wizard instead
- We would get access to GPT-4o and newer vision models for better OCR on
  low-contrast diagrams
- No change to the overall architecture

The preanalyze + cache pattern is correct for technical-manual-scale RAG in
any Azure cloud.

---

## 7. What happens for 500-page and 2000-page PDFs with heavy diagrams

Realistic scenarios we've seen or expect:

### 500-page PDF with ~1,500 figures (typical electrical/mechanical manual)

| Phase | Duration | Notes |
|---|---|---|
| Preanalyze DI | ~5-10 min | 30MB+ PDF sent via SAS URL to avoid POST body limits |
| Preanalyze crop upload | ~1-3 min | Parallel upload of ~1,000 crops after triage |
| Preanalyze vision | ~5-20 min | 40 parallel GPT-4V calls |
| Index (per-PDF contribution) | ~5-15 min | Hits fast cache path, dominated by embeddings |
| **Total end-to-end** | **~30-60 min** | First-time ingestion |

Re-runs (after bugfix, schema change, etc.):
- Preanalyze incremental: seconds (skips fully-cached PDFs)
- Index rerun: minutes (reads cache, re-embeds if schema changed)

### 2,000-page PDF with ~6,000 figures (rare edge case, massive reference manual)

| Phase | Duration | Notes |
|---|---|---|
| Preanalyze DI | ~15-30 min | At the upper edge of DI's server-side limits |
| Preanalyze crop upload | ~5-10 min | |
| Preanalyze vision | ~30-90 min | Mostly bound by AOAI TPM quota |
| Index (per-PDF contribution) | ~15-30 min | |
| **Total end-to-end** | **~2-4 hours** | |

At this scale a few things matter:

- **AOAI deployment TPM quota** is the throughput ceiling. S0 deployments
  cap around 240K TPM for ada-002 and ~80K TPM for GPT-4 vision. For 6,000
  figures the embedding phase alone needs ~1-2 hours on S0.
- **Azure Search indexer 24-hour execution cap** (on S1+ tiers) is not
  reached for a single PDF even at this size, but we still recommend smaller
  batches (one PDF at a time if possible).
- **Preanalyze parallelism**: --vision-parallel 40-48 usually saturates the
  AOAI quota without 429 storms. Higher numbers cause throttling.

### What breaks at scale and how we handle it

| Failure mode | Our mitigation |
|---|---|
| DI timeout on >100MB PDF | `urlSource` path with SAS URL (no POST body limit) |
| Vision 429 rate limit | Bounded Retry-After wait (cap 120s), retry 3x |
| Vision content filter | Cache as permanent failure, never retry |
| Vision JSON parse failure | Cache as transient, retry up to 3 times across runs |
| Cache blob upload flake | Retry 3x with backoff before treating as failure |
| Indexer skill crashes | Per-PDF failure doesn't stop the rest |
| One figure crash in a batch | Logged, next figure continues |
| Blob flake mid-run | All blob ops retry 3x; connection pools timeout at 600s |
| 230s skill timeout | Preanalyze: the whole point |

---

## 8. Text, diagrams, tables - how each becomes a record

### Text chunks

1. `DocumentIntelligenceLayoutSkill` produces section-level markdown
2. `SplitSkill` chops each section into overlapping 1200-char pages
3. `extract-page-label-skill` adds physical_pdf_page + printed_page_label
4. Embedding skill vectorizes the chunk_for_semantic field
5. Index projection writes one record per chunk with `record_type="text"`

Fields: chunk_id, parent_id, chunk, chunk_for_semantic, text_vector,
header_1/2/3, physical_pdf_page(s), printed_page_label(s), figure_ref,
source_file, source_url, skill_version.

### Diagram records

1. Preanalyze already wrote `_dicache/<pdf>.vision.<fig>.json`
2. `process-document-skill` emits `enriched_figures` array
3. `analyze-diagram-skill` runs per figure, reads cache, returns record
4. Embedding skill vectorizes the dgm_chunk_for_semantic field
5. Index projection writes one record with `record_type="diagram"`

Fields: same citation fields + figure_id, figure_bbox, figure_ref,
image_hash, has_diagram, diagram_description, diagram_category,
processing_status.

Key decisions:
- `image_b64` is **not** stored in the index (would bloat it 10-100x). The
  PNG stays in the `_dicache/` blob; the index stores only bbox and hash.
- `diagram_description` contains both the model's narrative description and
  the OCR labels, concatenated so a BM25 query for a part number can hit.
- `image_hash` dedupes identical figures (logos, repeated nameplates).

### Table records

1. DI gives us table cells + bounding regions
2. `tables.py` merges continuation tables, drops repeated header rows
3. Oversized tables (> 3000 chars) split at row boundaries
4. `process-table-skill` emits one record per (possibly split) table
5. Embedding skill vectorizes tbl_chunk_for_semantic

Fields: chunk_id, parent_id, chunk (markdown), table_caption,
table_row_count, table_col_count, physical_pdf_page(s), header_1/2/3,
source_file, source_url.

### Summary records (optional)

One per document. Generated from the first N sections. Useful for "tell me
what this manual is about" type queries.

---

## 9. Format decisions and why

### Why markdown instead of plain text

DI produces markdown by default, and headers survive as `# / ## / ###` so we
can extract `header_1`, `header_2`, `header_3` per chunk. Plain text would
lose the hierarchy and make navigation harder.

### Why chunk size 1200 with 200 overlap

- 1200 chars (~200-300 tokens) is small enough for precise retrieval and
  large enough to preserve local context
- 200 char overlap prevents sentence-boundary cuts losing important context
  at chunk edges
- The reranker downweights near-duplicates so overlap doesn't pollute results

### Why ada-002 for embeddings

- Available in both Commercial and Gov (including Gov's limited catalog)
- 1536 dimensions - good precision-to-cost ratio
- Cheapest per-token of the embedding family
- Hybrid (vector + BM25) retrieval with semantic reranker covers for any
  weakness of the embedding model

### Why GPT-4 Vision (not a smaller model)

- Smaller vision models (3.5-turbo-vision, etc.) don't read tiny technical
  labels reliably
- Nameplate OCR requires recognizing degraded, rotated, low-contrast text
- Technical diagrams have dense component-level information
- Cost is dominated by the number of calls, not the per-call token price,
  and we cache every result

---

## 10. Automation and ongoing operations

### Initial load (first 50 PDFs)

Manual one-time run:
1. `./scripts/run_preanalyze.ps1 -VisionParallel 48` -- expect 1-3 hours for 50 PDFs
2. Delete the existing index (clean slate)
3. `python scripts/deploy_search.py --config deploy.config.json --run-indexer`
4. Verify via portal Execution History that all 50 succeeded

### Steady state - weekly-ish additions and deletions

Two options, documented together so teams can choose:

#### Option A - scheduled batch (simplest)

An Azure Container App Job (or a Windows Task Scheduler on a VM) runs every
4 hours:

```
1. python scripts/preanalyze.py --config deploy.config.json --incremental
2. python scripts/preanalyze.py --config deploy.config.json --cleanup
3. az rest ... /indexers/<name>/run
```

Lag from upload to searchable: up to 4 hours.

#### Option B - event-driven (near real-time)

Event Grid fires on blob Created / Deleted -> queue -> Container App Job
worker -> preanalyze that PDF -> trigger indexer.

Lag from upload to searchable: ~5-15 minutes.

Requires:
- Event Grid subscription on the blob container
- Storage Queue for debouncing
- Container App Job configured for event-triggered execution
- Indexer configured with `NativeBlobSoftDeleteDeletionDetectionPolicy`
- Storage account with blob soft-delete enabled

### Deletion handling

Unless you add a deletion detection policy, PDFs deleted from the blob
**stay in the index forever**. To handle deletions:

1. Enable blob soft-delete on the storage account (Azure portal)
2. Add to `search/indexer.json`:
   ```json
   "dataDeletionDetectionPolicy": {
     "@odata.type": "#Microsoft.Azure.Search.NativeBlobSoftDeleteDeletionDetectionPolicy"
   }
   ```
3. Ensure something periodically runs `preanalyze.py --cleanup` to remove
   orphaned `_dicache/` blobs (otherwise your storage bill grows)

---

## 11. Trade-offs summary

| Decision | Reason | Trade-off accepted |
|---|---|---|
| Preanalyze offline | 230s skill timeout + DI duration + vision call count | Extra step before indexing; one more thing to monitor |
| Cache in blob storage | Cheap, durable, same account as source PDFs | Need to version-control cache schema via skill_version |
| GPT-4 Vision per figure | Deep technical understanding + OCR | Higher cost per figure; mitigated by cache |
| Custom Function App | Built-in skills can't do per-figure vision cache-aware | More infra to deploy and monitor |
| Hybrid vector + BM25 + semantic reranker | Covers embedding model weakness for technical jargon | Slightly higher query latency than pure vector |
| Single index field `text_vector` for all record types | Simpler projections; same ada-002 model | Can't filter by record_type before vector search (filter at search time instead) |
| Record per figure (not per-page) | Precise figure citations | More records in the index |
| Don't store image_b64 in index | Index stays small and cheap | Need separate blob access to fetch the PNG for display |
| 1200/200 chunk settings | Good balance for technical prose | Near-duplicate overlap; reranker handles it |

---

## 12. Frequently asked

### Can we use Azure AI Search's Import and Vectorize Data wizard?

For simple PDFs (mostly text, few figures) - yes, in Azure Commercial. Point
it at the blob container, pick defaults, done. This pipeline is overkill for
that case.

For technical manuals with per-figure vision requirements - no. The wizard's
built-in image handling produces generic tags only.

### Why do we have both preanalyze AND a custom skill that reads the cache?

Preanalyze populates the cache offline. The custom skill reads the cache at
index time. They have to be two separate things because the indexer is what
writes to the search index - preanalyze can't do that alone. The split also
lets us re-run indexing independently of re-running vision (e.g. after a
schema change).

### Can we put preanalyze inside an Azure Function Timer Trigger?

On Consumption plan (10 min timeout), no -- one large PDF exceeds 10 min.
On Premium plan (30 min timeout), yes for typical PDFs but a 2000-page
manual can still exceed 30 min.

Best fit: Azure Container App Job. No execution time limit, pay only when
running, straightforward Docker packaging.

### What if a content filter blocks a legitimate figure?

Preanalyze caches content-filter responses as permanent failures. The figure
will have `processing_status="content_filter"` and no vision description,
but it still appears in the index with its page, section headers, and crop
bounding box. Retrieval via text surrounding context still works for it.

### How do we know if something silently failed?

- Portal -> Search service -> Indexers -> Execution history -> Errors tab
  (for indexer-level failures)
- App Insights or `az webapp log tail` for Function App errors
- `preanalyze.py --status` for per-PDF cache state
- End-of-run error summary prints failed PDFs

If you see `processing_status` not equal to `"ok"` on a chunk in the index,
that's the skill telling you something was off for that specific record.

---

## 13. Related docs

- [README.md](../README.md) - top-level project readme
- [RUNBOOK.md §2](RUNBOOK.md#2-preanalyze-runbook) - team-facing
  runbook for running preanalyze
- [RUNBOOK.md §3](RUNBOOK.md#3-validation) - validation notes
- [search/skillset.json](../search/skillset.json) - full skillset definition
- [search/index.json](../search/index.json) - index schema
- [function_app/shared/](../function_app/shared/) - custom skill implementations

---

# 2. Bootstrap


End-to-end commands to take an environment from "Bicep just finished
provisioning" to "indexer is running and serving search results".
Every command is copy-paste ready. Where you have to fill in a value,
look for the inline `# fill in:` comment.

Total time: ~10 minutes of typing + 10 minutes of waiting + however
long preanalyze takes for your PDFs.

> **Prerequisites already done before you start this:**
> - Bicep template deployed; resources exist
> - Function App and Search service have system-assigned MI **enabled**
> - You ran `git clone` of this repo and `cd`-ed into it
> - `az login` already done
> - `deploy.config.json` already exists (copied from
>   `deploy.config.example.json` and filled in)

---

## Step 1 — Sync the repo to latest

```bash
git pull origin main
```

After this, `scripts/assign_roles.ps1` should be present.

---

## Step 2 — Confirm Azure context

```bash
az account show --query "{cloud:environmentName, sub:name}" -o table
```

Check the output:
- `cloud` should be `AzureCloud` (commercial) or `AzureCloud` (gov)
- `sub` should be the subscription that holds your resources

If the subscription is wrong:

```bash
az account set --subscription "<your-subscription>"   # fill in: subscription name or id
```

---

## Step 3 — List your resources to find their names

```bash
az resource list -g <your-rg> --query "[].{name:name, type:type}" -o table   # fill in: <your-rg>
```

You're looking for up to 7 names. Five are obvious from the type column;
the Cognitive Services accounts need one more query to tell them apart
by `kind`:

```bash
az cognitiveservices account list -g <your-rg> --query "[].{name:name, kind:kind}" -o table   # fill in: <your-rg>
```

Map the output:

| `kind` value | Means this is your |
|---|---|
| `OpenAI` | **AOAI** (must be a separate resource — AOAI deployments do not live in multi-service accounts) |
| `FormRecognizer` | Standalone **Document Intelligence** |
| `CognitiveServices` | **Azure AI multi-service** account |

Three valid layouts you might see in your environment:

| Layout | What you have | What to use for DI / AISVC |
|---|---|---|
| **Two separate accounts** | one `FormRecognizer` + one `CognitiveServices` | `DI` = the FormRecognizer name, `AISVC` = the CognitiveServices name |
| **One multi-service account** (common in **commercial Azure**, Azure Gov, and many enterprise environments) | one `CognitiveServices` only — DI is bundled inside it | `DI` and `AISVC` are **the same name** — the multi-service account |
| **Standalone DI only** (rare) | one `FormRecognizer` only | Provision a multi-service `CognitiveServices` account too — the built-in Layout skill needs it for billing |

Write down all the names you'll need (5 to 7 depending on layout). You'll paste them in the next step.

### commercial Azure / Gov Cloud — verify your AOAI model availability

commercial Azure lags Azure Commercial by 12–18 months on new models.
**Before you continue, confirm `gpt-4.1` is actually available in your
AOAI resource:**

```bash
az cognitiveservices account list-models \
  -n <your-aoai> -g <your-rg> \
  --query "[?contains(name, 'gpt-4') || contains(name, 'embedding')].{name:name, version:version}" \
  -o table   # fill in: <your-aoai>, <your-rg>
```

If `gpt-4.1` doesn't appear, use whatever vision-capable model is
available (typically `gpt-4o` or `gpt-4-turbo` in Gov), and update
`deploy.config.json` so `azureOpenAI.chatDeployment` and
`azureOpenAI.visionDeployment` reference the deployment name you
actually created.

---

## Step 4 — Edit `scripts/assign_roles.ps1`

Open the file in your editor. The top has this block:

```powershell
# ---------- FILL THESE IN ----------
$Rg      = '<your-rg>'                            # resource group name
$Search  = '<search-service-name>'                # Microsoft.Search/searchServices
$Storage = '<storage-account-name>'               # Microsoft.Storage/storageAccounts
$Aoai    = '<aoai-resource-name>'                 # cognitiveservices kind=OpenAI

# $Di + $AiSvc: if you have a standalone FormRecognizer AND a separate
# multi-service CognitiveServices account, fill in those two names.
# If you have only ONE multi-service CognitiveServices account (common
# in commercial Azure / Gov Cloud), set $Di and $AiSvc to the SAME name.
$Di      = '<di-or-multi-service-account-name>'
$AiSvc   = '<ai-services-multi-service-account-name>'

$Func    = '<function-app-name>'                  # Microsoft.Web/sites
# -----------------------------------
```

Replace each `<...>` with the actual name from Step 3. Don't change
anything below the dashed line. Save the file.

---

## Step 5 — Run the role assignments

```powershell
.\scripts\assign_roles.ps1
```

Expected output:

```
Looking up principal IDs and resource IDs...

A. Granting your user (deploying principal) roles...
  -> Search Service Contributor
  -> Search Index Data Contributor
  -> Storage Blob Data Contributor
  -> Cognitive Services OpenAI User
  -> Cognitive Services User

B. Granting Search service MI roles...
  -> Storage Blob Data Reader
  -> Cognitive Services OpenAI User
  -> Cognitive Services User

C. Granting Function App MI roles...
  -> Storage Blob Data Reader
  -> Cognitive Services OpenAI User
  -> Cognitive Services User
  -> Search Index Data Reader

All 12 role assignments submitted.
Wait 10 minutes for RBAC propagation before running deploy_search.py.
```

If you see `is still the placeholder` — you forgot to edit the names
at the top. Fix and re-run; the script is idempotent.

If you see `missing a system-assigned identity` — run the two `az`
commands the script prints, then re-run.

---

## Step 6 — Wait 10 minutes

Set a timer. RBAC propagation is 5–10 minutes; running early gives
you 403 even though the assignments are correct.

---

## Step 7 — Deploy the Function App code

If you haven't already done this earlier:

```bash
bash scripts/deploy_function.sh deploy.config.json
```

Or PowerShell:

```powershell
.\scripts\deploy_function.ps1 -Config .\deploy.config.json
```

This publishes the Python package and applies App Settings on the
Function App. Skip this step if you already ran it.

---

## Step 8 — Deploy search artifacts (datasource, index, skillset, indexer)

```bash
python scripts/deploy_search.py --config deploy.config.json
```

This is the step that previously gave you 403. With the role grants
from Step 5 in place and the 10-minute wait done, it should now print
four `ok` lines for `datasources/...`, `indexes/...`,
`skillsets/...`, `indexers/...`.

---

## Step 9 — Pre-analyze every PDF in the container

```bash
python scripts/preanalyze.py --config deploy.config.json
```

This is the long-running step. For a typical 500-page manual it takes
30–60 minutes. For an initial load of many PDFs, run phased:

```bash
python scripts/preanalyze.py --config deploy.config.json --phase di --concurrency 3
python scripts/preanalyze.py --config deploy.config.json --phase vision --vision-parallel 40
python scripts/preanalyze.py --config deploy.config.json --phase output
```

Cache lands in the same blob container under `_dicache/`.

---

## Step 10 — Trigger the indexer

```bash
python scripts/deploy_search.py --config deploy.config.json --run-indexer
```

The indexer will pick up every PDF, run the skillset over it, and
write records into the index. For a single 500-page manual this
takes ~10 minutes after preanalyze (because the cache makes the
custom skills fast).

---

## Step 11 — Validate

```bash
python scripts/smoke_test.py --config deploy.config.json
```

This waits for `status=success` on the indexer, then asserts record
counts, required fields, and that `physical_pdf_pages` covers the
declared start+end on text/table records. Non-zero exit on any
failure.

Optional sanity check on what's actually in the index:

```bash
python scripts/check_index.py --config deploy.config.json
```

Shows total docs, per-`record_type` breakdown, and flags any field
that's null on 100% of records (a schema/skillset drift signal).

---

## Done

If Steps 1–11 all succeeded, the index is live and queryable. The
indexer's schedule (`PT15M` by default) will keep picking up new
PDFs from now on.

For the production automation that handles add / update / delete
without manual intervention, see [RUNBOOK.md §16](RUNBOOK.md#16-production-automation--add--update--delete).

For failure-mode walkthroughs if any step misbehaved, see
[RUNBOOK.md §17](RUNBOOK.md#17-anticipated-failure-modes-and-runbooks).

---

# 3. RBAC


Every identity in the indexing pipeline and the exact role assignments
it needs. Audit this table before promoting to a new environment.

All assignments are **AAD / managed identity** unless noted otherwise.

---

## Function App managed identity

The Function App's system-assigned MI is the runtime identity that
calls every downstream service from inside skill code.

| Scope | Role | Why |
|-------|------|-----|
| Storage account (PDF container) | **Storage Blob Data Reader** | Read PDFs + cache blobs (`_dicache/*`) |
| Azure OpenAI | **Cognitive Services OpenAI User** | Embeddings + chat + vision |
| Document Intelligence | **Cognitive Services User** | DI live calls (only used as fallback when cache missing) |
| Azure Search | **Search Index Data Reader** | Hash-cache lookup against existing diagram records |
| AI Services multi-service account | **Cognitive Services User** | Built-in DocumentIntelligenceLayoutSkill billing |

---

## Search service managed identity

Used by the indexer + skillset to read PDFs, call AOAI for embeddings,
and authenticate the AI Services Layout skill.

| Scope | Role | Why |
|-------|------|-----|
| Storage account (PDF container) | **Storage Blob Data Reader** | Pull source PDFs into the indexer |
| Azure OpenAI | **Cognitive Services OpenAI User** | Embedding skill calls |
| AI Services account | **Cognitive Services User** | DocumentIntelligenceLayoutSkill (`AIServicesByIdentity`) |

---

## Jenkins agent identity

Used by `Jenkinsfile.deploy` and `Jenkinsfile.run` to deploy code,
deploy search artifacts, run preanalyze, and persist status. Either
System-Assigned MI on the agent VM, or a service principal whose
credentials are bound as a Jenkins credential.

| Scope | Role | Why |
|-------|------|-----|
| Function App resource group | **Contributor** | Deploy code + read function key (`az functionapp keys list`) |
| Storage account (PDF container) | **Storage Blob Data Contributor** | Read + delete cache blobs from `reconcile.py` and `preanalyze.py --cleanup` |
| Search service | **Search Service Contributor** | Create/update index, indexer, skillset, datasource |
| Search service | **Search Index Data Contributor** | Delete chunk records from `reconcile.py`, query for coverage reports |
| Azure OpenAI | **Cognitive Services OpenAI User** | preanalyze.py vision calls (live) |
| Azure OpenAI | **Cognitive Services Contributor** _OR_ key access | Read API key for vision calls. If the agent uses key auth, the key must be in a Jenkins secret credential. |
| Document Intelligence | **Cognitive Services User** | preanalyze.py DI submission |
| AI Services account | **Cognitive Services User** | Reading account info during deploy |
| Cosmos DB account | **Cosmos DB Built-in Data Contributor** | Write run history + per-PDF state |

> **Custom roles.** The Cosmos role above is the built-in data-plane
> role. If your tenant restricts it, create a custom role granting
> `Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers/items/*`
> on the `indexing` database.

---

## Local-developer identity (the human running scripts)

For ad-hoc runs from a workstation while debugging.

| Scope | Role | Why |
|-------|------|-----|
| Same as Jenkins agent | Same | Scripts behave identically; access controlled at the Azure level. |

Developers should run `az login --use-device-code` if interactive
browser auth is unavailable on their corporate workstation.

---

## How to apply

```bash
# Fish out the principal IDs
FUNC_PRINCIPAL=$(az functionapp identity show -g "<rg>" -n "<func>" --query principalId -o tsv)
SEARCH_PRINCIPAL=$(az search service show -g "<rg>" -n "<search>" --query identity.principalId -o tsv)

# Storage example
STORAGE_ID=$(az storage account show -g "<rg>" -n "<storage>" --query id -o tsv)
az role assignment create \
    --assignee-object-id "$FUNC_PRINCIPAL" \
    --assignee-principal-type ServicePrincipal \
    --role "Storage Blob Data Reader" \
    --scope "$STORAGE_ID"
```

The script `scripts/assign_roles.ps1` automates this for the common
case; review and adapt before running.

---

## What to audit when handing off

Run this check periodically:

```bash
# Function App MI roles
az role assignment list --assignee "$FUNC_PRINCIPAL" --output table

# Search service MI roles
az role assignment list --assignee "$SEARCH_PRINCIPAL" --output table
```

Compare against this document. Missing assignments produce silent
failures (skills return empty results, indexer reports `success` with
0 items processed).

---

# 4. Jenkins


Two pipelines live in this repo:

| File | Trigger | What it does |
|------|---------|--------------|
| [`Jenkinsfile.deploy`](../Jenkinsfile.deploy) | Push to `main` (with manual approval before prod) | Deploy function app code → deploy search artifacts → smoke test |
| [`Jenkinsfile.run`](../Jenkinsfile.run) | Cron `0 2 * * *` UTC + manual button | Reconcile → preanalyze → wait for indexer → coverage → Cosmos status |

---

## One-time agent setup

You need a Linux Jenkins agent (Ubuntu 22.04 LTS recommended) with:

```bash
# Python 3.11+
sudo apt-get install -y python3.11 python3.11-venv python3-pip

# Azure CLI
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash

# Tell az to use commercial cloud
az cloud set --name AzureCloud

# LibreOffice (OPTIONAL but recommended) -- enables figure extraction
# from DOCX/PPTX/XLSX via auto-conversion to PDF. Without it those
# formats index for text + tables only.
sudo apt-get install -y libreoffice
```

`Jenkinsfile.run` attempts to auto-install LibreOffice on first run if
it's missing. The pipeline still works without it; figures from non-PDF
formats just won't be extracted.

Identity options (in order of preference):

1. **System-assigned MI on the agent VM** (production default).
   - Enable on the VM in the Azure portal.
   - Assign the roles in [SETUP.md §3](SETUP.md#3-rbac) to the VM's MI.
   - In Jenkins jobs, `az login --identity` with no further config.
2. **Service principal** (for cross-cloud agents or Jenkins running
   outside Azure).
   - Create the SP and assign the same roles.
   - Bind credentials as a Jenkins **Username + Password** credential
     where `username = AZURE_CLIENT_ID` and `password = client_secret`.
   - Add `AZURE_TENANT_ID` as a string credential.
   - In the pipeline, `withCredentials([...]) { az login --service-principal ... }`.

---

## Required Jenkins credentials

Both pipelines load `deploy.config.json` from a Jenkins **secret file**
credential so the per-environment identifiers never appear in code.

| Credential ID | Type | Contents |
|---------------|------|----------|
| `deploy-config-dev` | Secret file | dev-environment `deploy.config.json` |
| `deploy-config-prod` | Secret file | prod-environment `deploy.config.json` |

Create them:

> Manage Jenkins → Credentials → System → Global → Add Credentials → Kind: Secret file → ID: `deploy-config-prod` → upload the file.

If using service-principal auth, also create:

| Credential ID | Type | Contents |
|---------------|------|----------|
| `azure-sp` | Username with password | `AZURE_CLIENT_ID` / `client_secret` |
| `azure-tenant` | Secret text | `AZURE_TENANT_ID` |

And add an early stage to both Jenkinsfiles:

```groovy
withCredentials([
    usernamePassword(credentialsId: 'azure-sp',
                     usernameVariable: 'AZ_CLIENT', passwordVariable: 'AZ_SECRET'),
    string(credentialsId: 'azure-tenant', variable: 'AZ_TENANT')
]) {
    sh '''
        az login --service-principal -u "$AZ_CLIENT" -p "$AZ_SECRET" --tenant "$AZ_TENANT"
    '''
}
```

---

## Configuring the deploy pipeline

`Jenkinsfile.deploy` is a multi-branch / parameterised job:

> New Item → Multibranch Pipeline → Branch source: GitHub → Pipeline:
> by Jenkinsfile → Path: `Jenkinsfile.deploy`.

Manual parameters:

- `TARGET_ENV` — `dev` or `prod`. Picks the secret-file credential.
- `SKIP_SMOKE` — emergency only.

Production stage gates on a manual approval (`input` step). Set the
approver list to the operations team.

---

## Configuring the run pipeline

`Jenkinsfile.run` is a separate parameterised job (single branch, main):

> New Item → Pipeline → Pipeline script from SCM → Script Path:
> `Jenkinsfile.run`.

Cron is `0 2 * * *` UTC; tweak in the Jenkinsfile if your manuals
update on a different cadence.

Parameters:

- `TARGET_ENV` — same.
- `SKIP_RECONCILE` — useful if reconcile is misbehaving and you just
  want to run preanalyze + coverage.
- `SKIP_WAIT` — short-circuit the indexer wait; useful for quick
  status checks.
- `MAX_PURGES` — caps how many PDFs reconcile is allowed to delete in
  one run. Default 2. Raise intentionally.
- `MAX_WAIT_MINUTES` — how long to poll the indexer before giving up
  and reporting current state. Default 60.

`disableConcurrentBuilds()` is on — only one run at a time per
environment, to prevent two concurrent pipelines fighting over the
same indexer.

---

## What runs vs what's parallel

```
Jenkinsfile.deploy (push to main)         Jenkinsfile.run (nightly)
─────────────────────────────────         ─────────────────────────
1. Checkout                               1. Checkout
2. Bootstrap (venv, az login)             2. Bootstrap
3. Tests + lint (gate)                    3. Load config
4. Load config                            4. python scripts/run_pipeline.py
5. Approve prod                              ├── reconcile.py
6. Deploy function app                       ├── preanalyze.py --incremental
7. Deploy search artifacts                   ├── wait for indexer
8. Smoke test (gate)                         ├── check_index.py --coverage --write-status
                                             └── write run record to Cosmos
```

---

## Failure modes + Jenkins behavior

| Failure | Pipeline behavior | Operator action |
|---------|-------------------|-----------------|
| Tests fail | Deploy aborts before any Azure call | Fix the test or revert the commit |
| `deploy_function.sh` fails | Deploy aborts; function app may be partially updated | Re-run; the script is idempotent |
| `deploy_search.py` fails on placeholder | Deploy aborts with the missing key name | Add the missing field to `deploy.config.json` |
| Smoke test fails | Deploy marked failed; function + search artifacts already updated | Investigate via `python scripts/diagnose.py`; can roll back by reverting commit + redeploying |
| Reconcile finds > MAX_PURGES | Run pipeline finishes step with exit 2; pipeline continues but skips purges | Re-run with higher `MAX_PURGES` if intentional |
| Preanalyze partial failure (some PDFs FAIL) | Run pipeline continues; failures listed in stdout + Cosmos | Inspect Cosmos run record; re-run preanalyze for failed PDFs |
| Indexer wait timeout | Run pipeline reports current state and exits non-zero | Indexer is still running on its own schedule; next pipeline run will pick up where this one left off |

---

## Notifications

Both pipelines emit a final `post` block. Wire to your team's channels:

```groovy
post {
    failure {
        emailext to: 'ops-team@example.com',
                 subject: "FAIL: ${currentBuild.fullDisplayName}",
                 body: "${env.BUILD_URL}"
        // or
        slackSend channel: '#indexing-pipeline',
                  color: 'danger',
                  message: "Indexing pipeline failed: ${env.BUILD_URL}"
    }
}
```

(Not added by default; depends on which Jenkins plugins your team has.)

---

# 5. Dashboard spec


What the Power BI analyst needs to wire indexing-pipeline tiles onto
the existing Cosmos-DB-backed dashboard.

---

## Data sources

Two new containers in the existing Cosmos DB account
(auto-created on first run; partition key shown):

### `indexing_run_history`

Partition key: `/partitionKey` (set to date `YYYY-MM-DD`).

One document per pipeline / preanalyze / reconcile / coverage run.

```json
{
  "id": "2026-05-04T02:30:00Z-a1b2c3d4",
  "partitionKey": "2026-05-04",
  "started_at": "2026-05-04T02:30:00Z",
  "ended_at":   "2026-05-04T03:14:00Z",
  "duration_seconds": 2640,
  "run_type": "full_pipeline | preanalyze | reconcile | coverage",
  "triggered_by": "jenkins-cron | jenkins-manual | manual",
  "git_sha": "b6f8473",
  "exit_code": 0,
  "steps": {
    "reconcile":   { "exit_code": 0 },
    "preanalyze":  { "exit_code": 0 },
    "wait_indexer":{ "last_status": "success", "items_processed": 56, "errors": 0, "warnings": 2 },
    "check_index": { "exit_code": 0 }
  },

  "blob_pdfs_total":   56,
  "fully_chunked":     54,
  "partial":            1,
  "not_started":        1,
  "orphaned":           0,
  "total_chunks":   92847,

  "pdfs_processed":     2,
  "pdfs_skipped":      54,
  "pdfs_failed":        0,

  "added":   ["new_manual.pdf"],
  "edited":  ["GAS_Procedure.pdf"],
  "deleted": [],
  "chunks_purged":     412,
  "cache_blobs_purged": 18,

  "errors": []
}
```

Not every field is populated for every run type — `run_type` tells you
which subset to expect:

- `full_pipeline` → `steps`, `blob_pdfs_total`, `fully_chunked`, `partial`, `not_started`, `total_chunks`, `exit_code`, `duration_seconds`
- `preanalyze` → `pdfs_processed`, `pdfs_skipped`, `pdfs_failed`, `errors`, `phase`, `incremental`
- `reconcile` → `added`, `edited`, `deleted`, `chunks_purged`, `cache_blobs_purged`, `errors`
- `coverage` → `blob_pdfs_total`, `fully_chunked`, `partial`, `not_started`, `orphaned`, `total_chunks`

### `indexing_pdf_state`

Partition key: `/partitionKey` (set to `source_file`, so each PDF lives
in its own logical partition).

One document per PDF, replaced on each pipeline run.

```json
{
  "id": "GAS_Procedure.pdf",
  "partitionKey": "GAS_Procedure.pdf",
  "source_file": "GAS_Procedure.pdf",
  "status": "done",
  "chunks_in_index": 1842,
  "last_indexed_at": "2026-05-04T03:14:00Z",
  "last_blob_modified": "2026-05-04T01:18:23Z",
  "last_error": null,
  "updated_at": "2026-05-04T03:14:00Z"
}
```

`status` enum: `done | partial | not_started | failed`.

---

## Recommended tiles

### Tile 1 — Coverage headline (KPI card × 4)

Read most-recent `coverage` or `full_pipeline` doc:

| KPI            | Source                         |
|----------------|--------------------------------|
| Manuals total  | `blob_pdfs_total`              |
| Fully indexed  | `fully_chunked` (% of total)   |
| Partial        | `partial`                      |
| Not started    | `not_started`                  |

Color rule: green if `fully_chunked / blob_pdfs_total >= 0.95`, yellow
between 0.8 and 0.95, red below.

### Tile 2 — Last run summary (table)

Read most-recent `full_pipeline` doc:

| Column          | Source                                  |
|-----------------|-----------------------------------------|
| Run started     | `started_at`                            |
| Triggered by    | `triggered_by`                          |
| Duration        | `duration_seconds`                      |
| Items processed | `steps.wait_indexer.items_processed`    |
| Errors          | `steps.wait_indexer.errors`             |
| Warnings        | `steps.wait_indexer.warnings`           |
| Git sha         | `git_sha`                               |

### Tile 3 — Run history trend (line chart)

X axis: `ended_at` (last 30 days)
Y axis: `fully_chunked`
Filter: `run_type == "full_pipeline" or run_type == "coverage"`

Visualises whether coverage is growing or stalling.

### Tile 4 — Per-PDF status (table)

Source: entire `indexing_pdf_state` container.

| Column           | Source            |
|------------------|-------------------|
| Manual           | `source_file`     |
| Status           | `status`          |
| Chunks in index  | `chunks_in_index` |
| Last indexed     | `last_indexed_at` |
| Last error       | `last_error`      |

Sort: status descending (so partial / failed bubble up), then
`last_indexed_at` ascending.

### Tile 5 — Recent errors (table)

Source: `indexing_run_history` where `len(errors) > 0`, last 7 days.

| Column     | Source           |
|------------|------------------|
| Run type   | `run_type`       |
| When       | `ended_at`       |
| Triggered by | `triggered_by` |
| Error      | First entry of `errors[]` |

### Tile 6 — Reconcile activity (last 7 days)

Source: `indexing_run_history` where `run_type == "reconcile"`.

Bar chart, X = day, three series:
- Added (new PDFs)
- Edited (re-indexed PDFs)
- Deleted (purged PDFs)

Optional: a stat on chunks_purged to show how much "cleanup" the
pipeline does over time.

---

## SQL examples

The Power BI Cosmos DB connector uses Cosmos SQL. Two starter queries:

**Most recent coverage snapshot:**

```sql
SELECT TOP 1 *
FROM c
WHERE c.run_type IN ("full_pipeline", "coverage")
ORDER BY c.ended_at DESC
```

**Per-PDF state with error sort:**

```sql
SELECT
  c.source_file,
  c.status,
  c.chunks_in_index,
  c.last_indexed_at,
  c.last_error
FROM c
ORDER BY c.status DESC, c.last_indexed_at ASC
```

**Run-failure rate over last 30 days:**

```sql
SELECT
  COUNT(1) AS runs,
  SUM(c.exit_code != 0 ? 1 : 0) AS failures
FROM c
WHERE c.run_type = "full_pipeline"
  AND c.ended_at > "2026-04-04T00:00:00Z"
```

---

## Identity / connection

Power BI to Cosmos requires the gateway to authenticate. Either:

- Cosmos DB **read-only key** stored in the analyst's Power BI
  workspace credential (simpler, but a key in flight).
- **Microsoft Entra ID** auth via a service principal granted
  `Cosmos DB Built-in Data Reader` on the database.

The latter is preferred — same pattern as the rest of the pipeline.

---

## Refresh cadence

Set Power BI dataset refresh to 30 minutes (or whatever cadence
matches the pipeline frequency). The pipeline runs nightly at 02:00,
so a single refresh at 03:30 suffices for the morning view; more
frequent refreshes give responsiveness on manual runs.

---

# 6. Search index reference


A plain-language reference for the Azure AI Search index that powers this
RAG system. Read this when you need to understand what the index holds,
how search works against it, what every field means, and which skills
transform PDFs into indexed records.

For the end-to-end pipeline architecture (preanalyze, custom skills,
cache), see [SETUP.md §1](SETUP.md#1-architecture). This guide focuses on
the search/index side only.

---

## 1. What is a search index?

A search index is a database optimized for finding things by text and
meaning, not by primary key. Azure AI Search indexes are:

- **Schema-defined**: you declare fields (name, type, and properties).
- **Inverted**: each word is mapped to documents that contain it (for
  fast text search).
- **Vectorized**: each record can carry a vector embedding for
  semantic/similarity search.
- **Filterable / sortable / facetable**: fields can be marked so the UI
  can slice by category, page range, date, etc.

An index lives inside an Azure AI Search service (our service name is
configured in `deploy.config.json` under `search.endpoint`).

## 2. What is search?

Search here means three things working together:

| Mode | How it finds results | Best at |
|---|---|---|
| **Keyword (BM25)** | Inverted index + TF-IDF-like ranking | Exact part numbers, product codes, literal phrases |
| **Vector** | Embeddings + cosine similarity (HNSW graph) | Conceptual queries, paraphrases, synonyms |
| **Semantic reranker** | L2 reranking on top of keyword/vector hits using Microsoft's transformer model | Lifting the most relevant chunks to the top, even when they would have been ranked lower by BM25/vector alone |

We use all three in **hybrid mode**: the search query is issued as both
keyword + vector, the results are merged, and the semantic reranker
re-orders the top 50 before returning.

This combination handles a wide range of technical-manual queries:

- "W130537" -> keyword (exact part number)
- "how do I install a BUD cable?" -> vector + reranker
- "underground distribution tables on page 18" -> keyword + filter on page

## 3. Index vs indexer vs skillset

Three related but distinct things:

| Object | Purpose | Lives in |
|---|---|---|
| **Index** | The schema + data store. Holds every chunk/record a user can search. | `search/index.json` |
| **Data source** | Points at the blob container that holds the source PDFs. | `search/datasource.json` |
| **Skillset** | An ordered pipeline of transforms. Runs on each source blob to turn it into one or more index records. | `search/skillset.json` |
| **Indexer** | The scheduler + executor. Pulls blobs from the data source, runs the skillset over each, and writes results to the index. | `search/indexer.json` |

Runtime relationship:
```
  Blob container            Data source -> Indexer -> Skillset
  (PDFs, _dicache)                         |              |
                                           v              v
                                           Index         writes records
                                           (our schema)
```

## 4. Our index schema, field by field

Schema source of truth: [search/index.json](../search/index.json).

### 4.1 Identity fields

| Field | Type | Purpose |
|---|---|---|
| `id` | string | Record key. Auto-generated, unique per chunk. |
| `chunk_id` | string | Stable human-readable id for the chunk. Shared across re-indexing if content is unchanged. |
| `parent_id` | string | Hash of the source PDF URL. Groups all records from one PDF. |
| `text_parent_id`, `dgm_parent_id`, `tbl_parent_id`, `sum_parent_id` | string | Only one is populated, per record. They tell you the record-type's parent pointer. Useful for "show me every record from this PDF that came from the text path". |
| `record_type` | string | `text`, `diagram`, `table`, or `summary`. Filter on this to restrict queries to a type. |

### 4.2 Content fields

| Field | Type | Purpose |
|---|---|---|
| `chunk` | string, searchable | Raw chunk text (markdown for text chunks, GPT-4V description for diagrams, markdown for tables). Primary content users see in the citation UI. |
| `chunk_for_semantic` | string, searchable | Chunk augmented with source + section headers + page info, tuned for semantic ranking. |
| `text_vector` | Collection(Single), 1536 dims | ada-002 embedding of `chunk_for_semantic`. Used for vector search. Not retrievable to keep payloads small. |

### 4.3 Page + location fields

| Field | Type | Purpose |
|---|---|---|
| `physical_pdf_page` | int | First physical page number in the PDF (1-indexed). For citation links like "jump to page 42". |
| `physical_pdf_page_end` | int | Last physical page. For chunks that span pages. |
| `physical_pdf_pages` | Collection(int), filterable/facetable | Every physical page this chunk touches. Use with `any(p: p eq 42)` filters. |
| `printed_page_label` | string | The label as printed on the page (`"iv"`, `"18-33"`, `"A-12"`). For reader-friendly citations. |
| `printed_page_label_end` | string | End label for multi-page chunks. |
| `layout_ordinal` | int | The DI section's ordinal position in the document. Useful for reconstructing document order. |

### 4.4 Section header chain

| Field | Type | Purpose |
|---|---|---|
| `header_1` / `header_2` / `header_3` | string | The h1/h2/h3 heading chain the chunk sits under. E.g. `"Chapter 18"` / `"18.2 Fusing"` / `"18.2.1 General"`. Used for breadcrumb citations and semantic ranking hints. |

### 4.5 Diagram-only fields

Populated for `record_type = "diagram"` records:

| Field | Type | Purpose |
|---|---|---|
| `figure_id` | string | DI-assigned figure identifier (e.g. `"134.3"`). |
| `figure_ref` | string | Human reference as the manual writes it (e.g. `"Figure 18.117"`). Often differs from figure_id. |
| `figure_bbox` | string (JSON) | `{page, x_in, y_in, w_in, h_in}`. Used by the UI to highlight the figure inside the source PDF page. |
| `diagram_description` | string, searchable | GPT-4V description + OCR labels. The content a user actually reads when they click a diagram result. |
| `diagram_category` | string, filterable (keyword analyzer) | One of: `circuit_diagram`, `wiring_diagram`, `schematic`, `line_diagram`, `block_diagram`, `pid_diagram`, `flow_diagram`, `control_logic`, `exploded_view`, `parts_list_diagram`, `nameplate`, `equipment_photo`, `decorative`, `unknown`. |
| `has_diagram` | bool | True only if `is_useful && category in useful set && description is non-empty`. Use as a filter to exclude junk. |
| `image_hash` | string | SHA-256 of the cropped PNG. Used for dedup of repeated logos/symbols. |

### 4.6 Table-only fields

Populated for `record_type = "table"` records:

| Field | Type | Purpose |
|---|---|---|
| `table_row_count` | int | Number of data rows in this chunk (after splitting oversized tables). |
| `table_col_count` | int | Number of columns. |
| `table_caption` | string, searchable | The caption text above the table in the source PDF. |

### 4.7 Source-reference fields

| Field | Type | Purpose |
|---|---|---|
| `source_file` | string, searchable/filterable/sortable/facetable | Just the PDF filename. For faceted navigation ("filter to this manual"). |
| `source_url` | string | Full blob URL. The UI links users to the source blob. |
| `source_path` | string, filterable | Same as source_url. Used in filter expressions. |

### 4.8 Provenance + health

| Field | Type | Purpose |
|---|---|---|
| `surrounding_context` | string, searchable | A few sentences around the figure/table from the surrounding body text. Makes results readable without opening the PDF. |
| `processing_status` | string, filterable/facetable | `"ok"`, `"no_image"`, `"content_filter"`, etc. Filter to `eq 'ok'` for clean retrieval. |
| `skill_version` | string | Tracks which version of the custom skills wrote the record. Useful when detecting mixed old/new records after schema changes. |

### 4.9 Admin classification fields (currently null)

Recently added to the schema for future classification. Populated via
REST API or a future skill, not by the current pipeline:

| Field | Type | Purpose |
|---|---|---|
| `operationalarea` | string, searchable | Operational area the PDF belongs to (e.g. `"underground_distribution"`). |
| `functionalarea` | string, searchable | Functional scope (e.g. `"power_management"`). |
| `doctype` | string, searchable | Document type (e.g. `"manual"`, `"policy"`, `"drawing"`). |

Leave these null until you add a classification step -- the pipeline
doesn't write to them today.

## 5. Semantic configuration

Semantic ranker settings from [search/index.json](../search/index.json):

```json
"semantic": {
  "defaultConfiguration": "mm-semantic-config",
  "configurations": [{
    "name": "mm-semantic-config",
    "prioritizedFields": {
      "titleField": { "fieldName": "source_file" },
      "prioritizedContentFields": [
        { "fieldName": "chunk_for_semantic" },
        { "fieldName": "chunk" },
        { "fieldName": "diagram_description" },
        { "fieldName": "surrounding_context" }
      ],
      "prioritizedKeywordsFields": [
        { "fieldName": "header_1" }, { "fieldName": "header_2" },
        { "fieldName": "header_3" }, { "fieldName": "figure_ref" },
        { "fieldName": "table_caption" }, { "fieldName": "printed_page_label" },
        { "fieldName": "diagram_category" }
      ]
    }
  }]
}
```

Interpretation:
- The ranker treats `source_file` as the "title" of each record.
- Content-priority fields are re-read with extra attention by the ranker.
- Keyword-priority fields bias matches for header chain, figure ref,
  table caption, page label, and category.

## 6. Vector search configuration

```json
"vectorSearch": {
  "algorithms": [{
    "name": "mm-hnsw-algo",
    "kind": "hnsw",
    "hnswParameters": { "m": 8, "efConstruction": 400, "efSearch": 500, "metric": "cosine" }
  }],
  "profiles": [
    { "name": "mm-hnsw-profile", "algorithm": "mm-hnsw-algo", "vectorizer": "aoai-vectorizer" }
  ],
  "vectorizers": [{
    "name": "aoai-vectorizer",
    "kind": "azureOpenAI",
    "azureOpenAIParameters": {
      "resourceUri": "<AOAI endpoint>",
      "deploymentId": "<your embedding deployment>",
      "modelName": "text-embedding-ada-002"
    }
  }]
}
```

- **HNSW**: approximate nearest neighbor graph. Fast vector search.
- **m=8, efConstruction=400, efSearch=500**: conservative graph that
  trades slightly higher build time for strong recall.
- **cosine**: the metric for embeddings this model produces.
- **vectorizer**: at query time the search service embeds the query text
  itself using the same ada-002 model. You don't need to embed queries
  client-side.

## 7. Our skillset, skill by skill

Skillset source of truth: [search/skillset.json](../search/skillset.json).

The skillset runs for every source PDF the indexer sees. Skills execute
in dependency order.

### 7.1 Built-in skills (no code, configured via JSON)

#### layout-skill (DocumentIntelligenceLayoutSkill)

- **Input**: the PDF file bytes.
- **Output**: a collection `markdownDocument[*]`, each with `content`
  (section markdown), and `sections` (h1/h2/h3 objects).
- **Purpose**: turn the PDF into markdown while preserving header
  structure and page markers.

#### split-pages (SplitSkill)

- **Input**: each section's `content` (markdown).
- **Output**: an array `pages` of ~1200-char overlapping chunks.
- **Purpose**: break long sections into chunks small enough to embed and
  retrieve precisely, with a 200-char overlap so sentence boundaries
  don't cut important context.

#### embed-text-chunks, embed-diagram-chunks, embed-table-chunks, embed-summary (AzureOpenAIEmbeddingSkill)

- **Input**: the chunk_for_semantic string for the record type.
- **Output**: a 1536-dim vector.
- **Purpose**: produce embeddings used by the index's `text_vector`
  field for vector search.

### 7.2 Custom skills (our Function App)

#### process-document-skill

- **Context**: once per source PDF.
- **Reads**: the DI cache blob (`_dicache/<pdf>.di.json`) written by
  preanalyze.
- **Outputs**:
  - `enriched_figures` - one entry per qualifying figure with cached
    crop + DI metadata.
  - `enriched_tables` - one entry per table (possibly split for size)
    with markdown + page range + caption.
  - `processing_status` - ok or needs_preanalyze.
- **Fails fast** if the cache is missing (we removed the live-DI fallback
  because it always timed out on big PDFs).

#### extract-page-label-skill

- **Context**: one call per text chunk (after split-pages).
- **Reads**: chunk text, section content, header_1/2/3 (for section matching).
- **Outputs**: `physical_pdf_page`, `physical_pdf_page_end`,
  `physical_pdf_pages`, `printed_page_label`, `printed_page_label_end`,
  `chunk_id`, `parent_id`, `record_type="text"`, `figure_ref`,
  `processing_status`, `skill_version`.
- **Purpose**: attach precise page info to every text chunk, using the
  DI cache as a fallback when the Layout Skill doesn't expose section
  pageNumber directly.

#### analyze-diagram-skill

- **Context**: one call per figure in `enriched_figures`.
- **Reads**: the pre-cropped image (from cache) and the pre-computed
  vision result (from `_dicache/<pdf>.vision.<fig>.json`).
- **Outputs**: `chunk_id`, `parent_id`, `record_type="diagram"`,
  `figure_id`, `figure_bbox`, `physical_pdf_page/end`, `header_1/2/3`,
  `diagram_description`, `diagram_category`, `figure_ref`,
  `has_diagram`, `image_hash`, `processing_status`, `skill_version`.

#### shape-table-skill

- **Context**: one call per table in `enriched_tables`.
- **Outputs**: `chunk_id`, `parent_id`, `record_type="table"`,
  `table_caption`, `table_row_count`, `table_col_count`,
  `physical_pdf_page/end`, `physical_pdf_pages`, `header_1/2/3`,
  `processing_status`, `skill_version`.

#### build-semantic-string-text, build-semantic-string-diagram

- **Context**: once per chunk / figure.
- **Outputs**: the `chunk_for_semantic` string that the embedding skill
  then vectorizes. We build it to include source + header chain + page
  info + caption, which the semantic ranker uses as signal.

#### build-doc-summary-skill

- **Context**: once per PDF.
- **Outputs**: a document-level summary record with `record_type="summary"`.
- **Purpose**: enables "tell me what this manual is about" queries.

### 7.3 Index projections

Projections map the skillset's nested outputs into flat index records.
Four parent-key selectors:

| Selector | Produces records with | record_type |
|---|---|---|
| `text_parent_id` | One per text chunk | `"text"` |
| `dgm_parent_id` | One per figure | `"diagram"` |
| `tbl_parent_id` | One per table | `"table"` |
| `sum_parent_id` | One per PDF | `"summary"` |

## 8. How to create (deploy) the index

You don't hand-create the index. It's deployed from `search/index.json`
via the deploy script.

```powershell
# Pre-req: az login + deploy.config.json filled in
python scripts/deploy_search.py --config deploy.config.json
```

What the script does:

1. Reads `deploy.config.json`.
2. Fetches the Function App host + function key.
3. Reads all four JSON files (`datasource.json`, `index.json`,
   `skillset.json`, `indexer.json`).
4. Substitutes placeholders (`<INDEX_NAME>`, `<FUNCTION_APP_HOST>`,
   `<FUNCTION_KEY>`, etc.) with real values.
5. PUTs each artifact to the Azure AI Search REST API using AAD auth.

Existing artifacts with the same name get updated in place. To rebuild
from scratch you must delete the index first (portal or REST DELETE).

### Reset and re-run the indexer

After you deploy (or after a schema change), reset the indexer so
it re-processes every blob:

```powershell
./scripts/reset_indexer.ps1
```

Or in the Azure portal: Search service -> Indexers -> your indexer ->
Reset -> then Run.

## 9. What's in our current index (summary)

Our index name is configured by `search.artifactPrefix` in
`deploy.config.json` (e.g. `mm-manuals-index`, or `techmanuals-v03-index`).

A fully populated run typically has:

- One `summary` record per PDF
- Hundreds to thousands of `text` records per PDF (every split chunk)
- One `diagram` record per useful figure
- One `table` record per extracted table (possibly split for size)

Sanity checks you can run:

```powershell
# Count total records (run in portal's Search explorer)
?search=*&$count=true&$top=0

# Count per record_type
?search=*&facet=record_type,count:5

# Count per source_file
?search=*&facet=source_file,count:50

# Quick query
?search=what is BUD&queryType=semantic&semanticConfiguration=mm-semantic-config&answers=extractive|count-3&captions=extractive&top=5
```

## 10. How to query the index

### Simple text search

```http
POST /indexes/<index-name>/docs/search?api-version=2024-11-01-preview
Authorization: Bearer <aad-token>

{
  "search": "buried underground distribution"
}
```

### Semantic + hybrid (recommended for user-facing queries)

```json
{
  "search": "buried underground distribution",
  "queryType": "semantic",
  "semanticConfiguration": "mm-semantic-config",
  "captions": "extractive",
  "answers": "extractive|count-3",
  "vectorQueries": [{
    "kind": "text",
    "text": "buried underground distribution",
    "fields": "text_vector"
  }],
  "top": 10
}
```

### Restrict to diagrams only

```json
{
  "search": "fault indicator",
  "filter": "record_type eq 'diagram' and has_diagram eq true",
  "top": 20
}
```

### Restrict to a page range within one PDF

```json
{
  "search": "fusing",
  "filter": "source_file eq 'ED-ED-UGC.pdf' and physical_pdf_page ge 1000 and physical_pdf_page le 1100"
}
```

### Find every page a chunk touches (collection filter)

```json
{
  "search": "*",
  "filter": "physical_pdf_pages/any(p: p eq 1337)"
}
```

### Find a specific figure

```json
{
  "search": "*",
  "filter": "figure_ref eq 'Figure 18.117' and record_type eq 'diagram'"
}
```

### Retrieve only what the citation UI needs

```json
{
  "search": "...",
  "select": "chunk_id, source_file, physical_pdf_page, printed_page_label, header_1, header_2, header_3, chunk, figure_bbox, record_type",
  "top": 5
}
```

## 11. Common operations

### Verify index health

```powershell
python scripts/check_index.py --config deploy.config.json
```

Shows document count, per-type breakdown, and flags any fields that are
null on 100% of records (a schema vs. skillset mismatch signal).

### Check indexer status

```powershell
az rest --method get `
  --url "https://<search>.search.windows.net/indexers/<name>/status?api-version=2024-11-01-preview" `
  --resource "https://search.windows.net" -o json
```

### Clear the index (keep schema, drop documents)

Azure AI Search doesn't have a native "truncate". To clear:

1. Delete the index (portal or REST DELETE).
2. Redeploy via `deploy_search.py`.
3. Reset and run the indexer to repopulate.

### Delete a single PDF's records

```http
POST /indexes/<index-name>/docs/index?api-version=2024-11-01-preview
{
  "value": [
    { "@search.action": "delete", "id": "<record-id>" }
  ]
}
```

Or query for all chunks of that PDF first:

```json
{ "search": "*", "filter": "source_file eq 'OLD.pdf'", "select": "id", "top": 10000 }
```

then batch-delete by id.

## 12. Troubleshooting

| Symptom | Likely cause | What to check |
|---|---|---|
| `text_vector` search returns nothing | Embedding dimension mismatch | Confirm `text_vector.dimensions == 1536` and the AOAI deployment is ada-002. |
| Text chunks have `physical_pdf_page: null` | Old data from before the DI-cache fallback fix | Redeploy function app, reset + run indexer. |
| Diagram records have `diagram_description: ""` | Vision API failed (rate limit or content filter) | Check `processing_status`; re-run preanalyze to retry transient failures. |
| Indexer shows 0 documents after 30 min | Indexer was fired before the `.pdf`-only filter was deployed OR first big PDF still processing | Check execution history; cancel + reset + run if using old config. |
| Wrong indexer index count vs. actual blobs | Azure CLI blob list defaulting to 5000 cap | Use `--num-results *` or count via the REST API. |
| `Execution time quota of 120 minutes reached` | Normal for large fresh loads | The 15-minute schedule auto-resumes. |

## 13. Related docs

- [SETUP.md §1](SETUP.md#1-architecture) - end-to-end pipeline design and
  why we preanalyze
- [RUNBOOK.md §2](RUNBOOK.md#2-preanalyze-runbook) -
  team-facing runbook for preanalyze
- [search/index.json](../search/index.json) - exact schema
- [search/skillset.json](../search/skillset.json) - exact skillset
- [search/indexer.json](../search/indexer.json) - indexer config
- [search/datasource.json](../search/datasource.json) - blob datasource
