# Architecture diagram — where everything lives and runs

A visual reference for "where does the code run, where does data live,
who talks to what." Use this when onboarding new team members or
when an incident makes you ask "is this thing on Jenkins or Azure?"

---

## The 5 environments

```
                     ┌──────────────────────────────────────┐
                     │                                      │
   YOUR LAPTOP       │             GITHUB                   │       JENKINS AGENT
                     │      (single source of truth         │       (corporate VM)
                     │       for code + search JSON)        │
                     │                                      │
   ┌──────────────┐  │  ┌─────────────────────────────┐    │       ┌──────────────┐
   │ git clone    │──┼─→│ /scripts/*.py                │    │       │ git clone    │
   │              │  │  │ /function_app/**/*.py        │←───┼───────│              │
   │ VS Code:     │  │  │ /search/*.json               │    │       │ Runs:        │
   │  - edit code │  │  │ /tests/*.py                  │    │       │  Jenkinsfile │
   │  - test      │  │  │ /docs/*.md                   │    │       │  .deploy or  │
   │  - debug     │  │  │ /Jenkinsfile.deploy          │    │       │  .run        │
   │              │  │  │ /Jenkinsfile.run             │    │       │              │
   │ git push ────┼──┼─→│                              │    │       │              │
   └──────────────┘  │  └─────────────────────────────┘    │       └──────┬───────┘
                     │                                      │              │
                     └──────────────────────────────────────┘              │
                                                                            │
                                                                            ▼
   ┌────────────────────────────────────────────────────────────────────────────────┐
   │                                                                                │
   │                              AZURE GOVERNMENT                                   │
   │                                                                                │
   │   ┌────────────────────┐     ┌────────────────────┐     ┌─────────────────┐  │
   │   │  STORAGE ACCOUNT   │     │  AZURE AI SEARCH   │     │  FUNCTION APP   │  │
   │   │                    │     │                    │     │                 │  │
   │   │  Container:        │     │  • Index           │←────│  Custom skills: │  │
   │   │  ┌──────────────┐  │     │    (records)       │     │  • process-doc  │  │
   │   │  │ manual_A.pdf │  │     │  • Skillset        │     │  • analyze-     │  │
   │   │  │ manual_B.pdf │  │     │    (config)        │ ←HTTP│    diagram     │  │
   │   │  │ ...          │  │     │  • Datasource      │  │  │  • shape-table  │  │
   │   │  │              │  │     │    (points→blob)   │  │  │  • build-summary│  │
   │   │  │ _dicache/    │←─┼─────│  • Indexer ───────┼──┘  │  • build-semantic│  │
   │   │  │  *.di.json   │  │     │    (orchestrator)  │     │                 │  │
   │   │  │  *.crop.*    │  │     │                    │     │  Runtime: Linux │  │
   │   │  │  *.vision.*  │  │     │  Reads: PDFs +     │     │  Python 3.11    │  │
   │   │  │  *.output.*  │  │     │  cache from blob   │     │  System MI      │  │
   │   │  └──────────────┘  │     │  Writes: index     │     │                 │  │
   │   └────────────────────┘     └────────────────────┘     └─────────────────┘  │
   │                                                                                │
   │   ┌────────────────────┐     ┌────────────────────┐     ┌─────────────────┐  │
   │   │  AZURE OPENAI      │     │  DOC INTELLIGENCE  │     │  COSMOS DB      │  │
   │   │                    │     │                    │     │                 │  │
   │   │  • text-embedding  │     │  • prebuilt-layout │     │  Database:      │  │
   │   │    -ada-002        │     │  • Returns:        │     │   indexing      │  │
   │   │  • gpt-4.1 vision  │     │    paragraphs,     │     │                 │  │
   │   │  • gpt-4.1 chat    │     │    figures, tables │     │  Containers:    │  │
   │   │                    │     │                    │     │  • run_history  │  │
   │   │  Called by:        │     │  Called by:        │     │  • pdf_state    │  │
   │   │   Indexer (embed), │     │   preanalyze.py    │     │                 │  │
   │   │   preanalyze.py    │     │   (offline)        │     │  Read by:       │  │
   │   │   (vision)         │     │                    │     │   Power BI,     │  │
   │   │                    │     │                    │     │   reconcile.py  │  │
   │   └────────────────────┘     └────────────────────┘     └─────────────────┘  │
   │                                                                                │
   └────────────────────────────────────────────────────────────────────────────────┘
```

---

## Who runs what — quick reference

| Action | Where it runs | Who triggers |
|---|---|---|
| Edit code | Your laptop (VS Code) | You |
| `git push` | Pushes to GitHub | You |
| `pytest` | Your laptop OR GitHub Actions CI | You / GitHub |
| `bootstrap.py` | Your laptop OR Jenkins agent | You / Jenkins push to main |
| `preanalyze.py` | Your laptop OR Jenkins agent | You / Jenkins nightly |
| `deploy_search.py` | Your laptop OR Jenkins agent (pushes to Azure Search) | You / Jenkins push to main |
| `deploy_function.sh` | Your laptop OR Jenkins agent (pushes to Function App) | You / Jenkins push to main |
| Custom skills (process-doc, etc.) | **Azure Function App** | Indexer calls them via HTTP |
| Indexer | **Azure Search service** (managed) | Schedule (every 15 min) or manual |
| `reconcile.py` | Your laptop OR Jenkins agent | You / Jenkins nightly |
| `check_index.py --coverage` | Your laptop OR Jenkins agent | You / Jenkins nightly |

---

## Where does the data live?

| Data | Lives in | Persistent? | Cost |
|---|---|---|---|
| Source PDFs | Storage account (blob container) | ✅ until you delete | Storage |
| Pre-analysis cache (`_dicache/`) | Same blob container | ✅ until you delete | Storage |
| Search records (text/diagram/table/summary) | Azure Search service (the index) | ✅ until you delete index | Search service tier |
| Embeddings (vectors) | Inside the search records | ✅ same as above | Search service tier |
| Run history (one row per pipeline run) | Cosmos DB `indexing_run_history` | ✅ optional | Cosmos RU |
| Per-PDF state | Cosmos DB `indexing_pdf_state` | ✅ optional | Cosmos RU |
| Logs / traces | Application Insights | 30 days default | App Insights ingestion |
| Code | GitHub | ✅ until repo deleted | Free (public) |

---

## The lifecycle of a single PDF

```
1. Content team uploads manual.pdf via Azure portal / az storage blob upload
        │
        ▼
   Lives in: storage_account/container/manual.pdf
        │
        ▼
2. (Either tonight via Jenkins, OR you trigger manually)
   preanalyze.py runs:
     - reads manual.pdf bytes from blob
     - calls Document Intelligence → markdown + figures + tables
     - PyMuPDF crops each figure → PNG
     - GPT-4 Vision analyzes each figure → description + OCR
     - writes _dicache/manual.pdf.{di,crop,vision,output}.json into the same container
        │
        ▼
3. Azure Search indexer runs (every 15 min on its own schedule):
     - sees manual.pdf in the container (HighWaterMark on lastModified)
     - runs the skillset:
        a. Built-in DI Layout → markdown
        b. SplitSkill → chunks
        c. WebApi: process-document (Function App) → reads cache, returns figures + tables
        d. WebApi: extract-page-label → page numbers
        e. WebApi: analyze-diagram (per figure) → reads cache, returns description
        f. WebApi: shape-table (per table) → markdown grid
        g. WebApi: build-semantic-string → assembles embedding input
        h. AOAI Embedding → 1536-dim vector
        i. WebApi: build-doc-summary → one summary record
        │
        ▼
   Index now has:
     - hundreds of "text" records
     - several "diagram" records (one per figure)
     - several "table" records
     - 1 "summary" record
        │
        ▼
4. End-user submits query through your AI assistant:
     - Query goes to Search service
     - Search runs hybrid (keyword + vector + semantic ranker)
     - Returns top chunks with citations
     - LLM generates answer + cites manual.pdf page X
```

---

## Two flows side by side: laptop vs Jenkins

### Flow A — You running locally (one-time setup or testing)

```
[YOUR LAPTOP]
    │
    ├──→ pip install -r requirements.txt    (your laptop's venv)
    │
    ├──→ az login                           (opens browser, your laptop session)
    │
    ├──→ python scripts/bootstrap.py
    │       │
    │       ├──→ az * commands          (your laptop → Azure REST API)
    │       ├──→ python preflight.py    (your laptop)
    │       ├──→ python assign_roles.py (your laptop → Azure)
    │       └──→ python deploy_search.py (your laptop → Azure Search)
    │
    ├──→ python scripts/preanalyze.py
    │       │
    │       ├──→ az storage blob list   (your laptop → Storage)
    │       ├──→ DI HTTP calls          (your laptop → DI service)
    │       ├──→ AOAI Vision calls      (your laptop → AOAI)
    │       └──→ blob upload            (your laptop → Storage)
    │
    └──→ Verify in Azure portal
```

**Implications:**
- Your laptop must reach all Azure services (network access)
- Your laptop's identity does the work
- Cache writes go through your laptop's network
- If you close your laptop mid-preanalyze, it stops; you re-run with `--incremental`

### Flow B — Jenkins running in production

```
[JENKINS AGENT VM, inside corporate network]
    │
    ├──→ git clone (from GitHub)
    │
    ├──→ pip install -r requirements.txt    (agent's venv)
    │
    ├──→ az login --identity                (managed identity)
    │
    ├──→ python scripts/bootstrap.py --auto-fix --skip-deploy-principal
    │       │
    │       └──→ Same scripts, but running on agent VM
    │
    ├──→ Triggered by:
    │     - Push to main → Jenkinsfile.deploy
    │     - Cron 02:00 → Jenkinsfile.run
    │     - Manual button → either
    │
    └──→ Logs in Jenkins console
```

**Implications:**
- Agent has stable network access (inside corp boundary)
- Agent's identity is consistent and managed
- Logs persist in Jenkins
- Runs unattended at scheduled times
- Multiple environments (dev/prod) use same code, different `deploy.config.json`

---

## What's in GitHub vs what's in Azure

| Item | GitHub | Azure |
|---|---|---|
| Python source code | ✅ scripts/, function_app/ | ❌ |
| Search artifact templates | ✅ search/*.json | ❌ |
| Tests | ✅ tests/ | ❌ |
| Docs | ✅ docs/ | ❌ |
| Jenkins pipelines | ✅ Jenkinsfile.* | ❌ |
| Per-environment config | ❌ (gitignored) | ❌ (Jenkins secret credentials, your laptop only) |
| Deployed function code | ❌ | ✅ Function App runtime |
| Deployed search index | ❌ | ✅ Search service |
| Deployed indexer / skillset | ❌ | ✅ Search service |
| PDF blobs | ❌ | ✅ Storage account |
| Pre-analyzed cache | ❌ | ✅ Storage account `_dicache/` |
| Search records | ❌ | ✅ Search service index |
| Run history | ❌ | ✅ Cosmos DB |

The split: **GitHub holds the truth about WHAT the system should do; Azure holds the truth about WHAT'S ACTUALLY DEPLOYED + WHAT DATA EXISTS.**

When `Jenkinsfile.deploy` runs, it reads from GitHub and pushes to Azure. They get back into sync.

---

## Frequently confusing questions

### "Where does my preanalyze cache go when I run it on my laptop?"

Into Azure Storage, the same container as your PDFs, under `_dicache/`. NOT to your laptop's disk.

```
storage_account/manuals/
  ├── manual_A.pdf       ← original
  ├── manual_B.pdf       ← original
  ├── _dicache/
  │   ├── manual_A.pdf.di.json        ← preanalyze wrote this
  │   ├── manual_A.pdf.crop.fig_1.json ← preanalyze wrote this
  │   ├── manual_A.pdf.vision.fig_1.json
  │   ├── manual_A.pdf.output.json
  │   └── ... (same for manual_B.pdf)
  └── _dicache/.lock-preanalyze.json  ← pipeline lock blob
```

This means:
- If Jenkins runs preanalyze tonight, then you check tomorrow on your laptop, you see Jenkins's cache.
- If you cache something on your laptop, Jenkins sees it next run.
- Your laptop is just a runner of scripts — it doesn't store state.

### "If I delete the index, do I need to re-run preanalyze?"

No — the cache survives. Just delete the index, redeploy artifacts, reset + run indexer. The indexer reads cache from blob and reconstructs the index in minutes (because the slow DI + Vision work is already done).

This is exactly why preanalyze + cache exists.

### "What if I push code changes to GitHub — do they auto-deploy?"

Only if Jenkinsfile.deploy is configured to trigger on push. By default it's a multibranch pipeline — push to main → Jenkins picks it up → runs the bootstrap → deploys.

Your laptop changes don't auto-deploy. You must `git push origin main`.

### "What runs every 15 minutes?"

The Azure Search indexer. It checks: "any blob with last_modified newer than my high-water-mark?" If yes, it processes those blobs through the skillset. If no (all unchanged), it's effectively a no-op and finishes in seconds.

### "What runs every night at 02:00?"

Jenkinsfile.run cron. It does the FULL pipeline:
- reconcile (purge stale chunks for deleted/edited PDFs)
- preanalyze --incremental (for new PDFs only)
- wait for indexer to settle
- write coverage to Cosmos

The indexer's 15-min schedule is independent of this.

### "If both Jenkins and the 15-min indexer run at the same time, do they conflict?"

The pipeline lock prevents Jenkins from running preanalyze + reconcile concurrently with another preanalyze. The Azure indexer is independent — it has its own internal locking and won't run two instances at once.

In practice: Jenkins reconciles + preanalyzes, then waits for indexer. Indexer runs concurrently with embedding computation but those are stateless. No conflicts at our scale.
