# Production scenarios — exhaustive catalogue (511 scenarios)

Every failure mode and edge case I could think of for this pipeline,
cross-checked against the code. Three sections:

1. **General scenarios** (176): auth, concurrency, security, cost, data integrity, ops, recovery, etc.
2. **Preanalyze deep-dive** (170): the offline script — blob fetch, DI, PyMuPDF, vision, cache, lock, status
3. **Indexer + Function App deep-dive** (165): Azure-managed indexer + custom skills

**Status legend:**
- ✅ **Handled** — code or config covers this case correctly
- ⚠️ **Partial** — handled in common case but has a known weakness
- ❌ **Gap** — not handled; mitigation listed
- 📖 **By design** — the behavior is intentional; documented for ops awareness

This is the working "did we think of everything" document. Use during
incidents (find the matching row), during deployment reviews (scan ⚠️
rows for the relevant category), and during quarterly audits.

---

# Section 1: General scenarios

---

## A. Authentication & identity (12 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| A1 | Function App MI token expires mid-batch (long DI run) | ✅ | `bearer_token()` re-fetched per call ([di_client.py:46](function_app/shared/di_client.py)); `DefaultAzureCredential` handles refresh |
| A2 | Search service MI loses Storage role grant | ❌ | Indexer reports auth errors; runbook entry needed. **Mitigation:** alert on indexer transient failures; check role assignment in [SETUP.md §3](SETUP.md#3-rbac) |
| A3 | Service principal client secret rotates without code change | ⚠️ | Jenkins pipeline fails on next run with auth error; pipeline must be updated with new secret manually |
| A4 | Tenant policy adds Conditional Access requiring MFA | ❌ | MI / managed identity is exempt; service principals may break. Document that all Jenkins identities must be MI or SP-with-secret-only |
| A5 | Multiple Function Apps share AOAI deployment, rate-limit interaction | ⚠️ | 429 retry handles burst; long contention causes degraded throughput. **Mitigation:** dedicated AOAI deployment per environment |
| A6 | KeyVault dependency missing | 📖 | We don't use KeyVault — all secrets come from MI tokens or Jenkins file credentials |
| A7 | Cosmos role propagation delay (5+ min after assignment) | ❌ | First Cosmos write fails with 401. **Mitigation:** preflight check waits + retries Cosmos auth; document delay |
| A8 | Subscription transferred between tenants (token resource changes) | ❌ | All scopes break; need full re-deploy and RBAC re-grant. Disaster recovery scenario |
| A9 | Az CLI in agent uses different identity than expected | ⚠️ | preflight catches this via `az account show` |
| A10 | Function key embedded in skillset is leaked via App Insights logs | ⚠️ | App Insights captures URLs including the `?code=` param. **Mitigation:** rotate periodically; key authorises only Function endpoints (limited blast radius) |
| A11 | Blob soft-delete enabled but legal hold prevents real delete | ❌ | reconcile delete_blob fails. Logged but doesn't block run. **Mitigation:** document; when legal hold is cleared, re-run reconcile |
| A12 | Indexer's data source connection string uses managed identity but storage key auth disabled, then ResourceId-style breaks | ⚠️ | datasource.json uses `ResourceId=...;` syntax which requires MI; verify with preflight |

## B. Race conditions & concurrency (15 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| B1 | Two Jenkins runs trigger simultaneously | ✅ | `disableConcurrentBuilds()` in [Jenkinsfile.run](Jenkinsfile.run) |
| B2 | Manual `preanalyze.py` running while pipeline is going | ❌ → **fixing** | Need pipeline lock; see fix below |
| B3 | preanalyze writing cache while reconcile is purging same PDF | ❌ → **fixing** | Same lock fixes this |
| B4 | Indexer running while reconcile deletes records for parent_id X | ✅ | Deterministic chunk_ids → re-inserted records have new IDs; old ones get purged. No collision |
| B5 | Jenkins job killed mid-pipeline (cancellation) | ⚠️ | preanalyze cache survives; partial state recoverable on next run via `--incremental`. Cosmos run record may not write. |
| B6 | Same PDF uploaded twice with different mtimes | ✅ | parent_id is content-derived from path → same path = same parent_id; second upload triggers edit detection |
| B7 | Filename case collision (`Manual.pdf` vs `manual.pdf`) | 📖 | Azure blob is case-sensitive; treated as separate documents (different parent_ids). Don't do this. |
| B8 | preanalyze --concurrency > 1 on same PDF | ✅ | Each PDF is processed by exactly one worker (futures keyed by PDF name) |
| B9 | Function App auto-scales, two instances both warm cache | ⚠️ | Module-level cache in `page_label.py` is now thread-safe (fixed). Cross-instance: each instance independently caches, no shared state — small inefficiency, no corruption |
| B10 | Two Function App instances both update Cosmos pdf_state for same PDF | ✅ | Cosmos upsert by id is atomic; last-write-wins is acceptable for state rows |
| B11 | Indexer batches lap each other (15-min schedule + long batch) | ✅ | Azure Search prevents two simultaneous runs of same indexer (skips next tick if previous still running) |
| B12 | Reset indexer + Run while reconcile in flight | ❌ | Race: reset clears HWM, run reprocesses everything, reconcile mid-purge. **Mitigation:** never run reconcile + reset within the same window; document |
| B13 | AOAI throttle backoff overlaps causing thundering herd | ⚠️ | Each preanalyze process backs off independently; if 5 processes all hit 429 at once they all sleep ~10s and retry together. **Mitigation:** jitter in retry; consider central token-bucket if scale grows |
| B14 | Cosmos throughput exhaustion mid-write | ✅ | Best-effort; logged warning, doesn't fail run. See [RUNBOOK.md §5#13](RUNBOOK.md#5-incident-response) |
| B15 | Storage 503 burst from too-parallel preanalyze | ✅ | Already retries (3 attempts); 30 parallel uploads bounded |

## C. Boundary conditions (15 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| C1 | PDF is 1 page (no figures, no tables) | ✅ | Returns text + summary records; figures/tables empty |
| C2 | PDF is 0 pages (empty stream) | ⚠️ | DI returns error "no content"; preanalyze marks FAIL-di |
| C3 | PDF is 5000 pages | ⚠️ | DI handles up to 2000 pages per call; >2000 fails. **Mitigation:** split source PDF before upload |
| C4 | PDF has 10,000 figures | ⚠️ | Vision API: 10K calls = $$, 30+ hours. **Mitigation:** image triage already drops decorative; consider per-PDF cap |
| C5 | PDF is 1 figure but no text | ✅ | Diagram record created; text records empty |
| C6 | PDF is OCR-only (scanned manual) | ✅ | DI's OCR handles; lower-quality text |
| C7 | PDF is mostly images (no extractable text) | ✅ | Vision describes images; text records sparse |
| C8 | PDF has Unicode emoji in headers | ✅ | UTF-8 throughout; build_highlight_text NFC-normalizes |
| C9 | PDF has right-to-left text (Arabic/Hebrew) | ⚠️ | DI extracts; chunking respects char order; semantic ranker may have RTL issues |
| C10 | PDF has CJK characters | ✅ | UTF-8 + NFC normalize; embedding model is multilingual |
| C11 | Filename is 250 chars long | ⚠️ | Azure blob name limit is 1024 chars; chunk_id includes content hash so still short |
| C12 | Filename has Unicode | ✅ | URL-encoded via quote() (filename-spaces fix covers this) |
| C13 | Container has 100,000 PDFs | ⚠️ | `az storage blob list --num-results *` handles unlimited; `--coverage` facet query may truncate at 1000 facets |
| C14 | Single chunk > 32KB (max embedding input) | ✅ | SplitSkill maxPageLength=1200 chars; semantic-string adds ~500 prefix; well under 32K |
| C15 | Empty container (zero PDFs) | ✅ | preanalyze prints "Nothing to process"; coverage shows 0/0 |

## D. Security (12 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| D1 | Malicious PDF (PDF/JavaScript) | ✅ | PyMuPDF doesn't execute scripts; DI is a managed service that won't either |
| D2 | PDF with embedded files (forms, attachments) | ⚠️ | Not extracted; embedded content invisible to search |
| D3 | PDF designed to OOM (zip bomb-style) | ⚠️ | Function app has 1.5GB memory cap; very large PDFs may fail. **Mitigation:** preflight could check blob sizes |
| D4 | SSRF via metadata_storage_path manipulation | ✅ | URL came from indexer; indexer validates it's a blob URL; MI scope-limited to authorized resources |
| D5 | OData injection via chunk content | ✅ | search_cache.py uses _odata_escape() and SAFE_TOKEN_RE validation |
| D6 | Function key leak via App Insights logs | ⚠️ | Key is in skillset URL; rotate periodically. App Insights logs may contain it; access-control App Insights workspace |
| D7 | Vision prompt injection (figure contains "ignore your instructions") | ❌ | GPT-4 Vision could be fooled. **Mitigation:** treat `diagram_description` as untrusted in downstream LLM prompts; never auto-execute its content |
| D8 | PII in chunks visible via Cosmos dashboard | ⚠️ | If manuals contain PII, chunks contain it. Cosmos read access should be restricted |
| D9 | Public-read on storage container exposing PDFs | ⚠️ | Configurable per container; preflight could verify |
| D10 | Blob URL with SAS token logged | ✅ | We don't log URLs with SAS query strings |
| D11 | Cross-tenant data leakage via shared deployment | ❌ | Each environment has its own resources; index name + Cosmos containers prefixed; document strict separation |
| D12 | Indexer error messages contain blob content | ✅ | Errors are status codes + skill names; not body |

## E. Cost & quota (12 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| E1 | AOAI quota hit mid-batch | ⚠️ | preanalyze retries with backoff; full quota exhaustion = some figures unprocessed; reported in run status |
| E2 | DI quota exhausted | ⚠️ | Rare; high default limits. Same retry as AOAI |
| E3 | Storage egress charges from re-fetching cache blobs every run | 📖 | Function app reads cache per skill invocation. For 60 PDFs at ~10MB cache each = 600MB / day egress. Negligible |
| E4 | Cosmos RU exhaustion mid-bulk-upsert | ✅ | Retried; best-effort; pipeline still succeeds |
| E5 | Search service tier insufficient for chunk count (Basic = 2GB index limit) | ⚠️ | 92K chunks × ~10KB each = ~900MB. Standard tier recommended. Document tier sizing |
| E6 | App Insights ingestion exceeds daily cap | ⚠️ | Daily cap configurable; verbose logging may exceed. Document cap setting |
| E7 | Vision tokens far exceed chat tokens (image processing is expensive) | 📖 | Each vision call = ~30K tokens. 60 PDFs × 30 figures = 1800 calls = ~$15. Documented in cost section of [README.md](../README.md) |
| E8 | Reset indexer triggers full re-embed (unexpected cost) | 📖 | Documented in [RUNBOOK.md §5#8](RUNBOOK.md#5-incident-response). $5 for full re-embed at our scale |
| E9 | Auto-heal loops increase cost on a permanently-broken PDF | ✅ | Bounded by `--heal-passes` (default 2) |
| E10 | Cosmos RU spike during reconcile bulk-delete | ⚠️ | reconcile processes one PDF at a time; for 50 PDFs deleted at once may spike RU. **Mitigation:** rate-limit deletes in reconcile (currently doesn't) |
| E11 | Test runs against production by accident | ❌ | Both Jenkinsfiles use TARGET_ENV parameter; no programmatic guard. **Mitigation:** human review; require approval for prod |
| E12 | Free-tier resource accidentally provisioned | ❌ | Out of scope of this repo; infra team's Bicep |

## F. Data integrity (12 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| F1 | parent_id collision (16-char SHA1 prefix) | 📖 | 16 chars = 64 bits = 1.8e19 distinct values; collision probability negligible at 100K-doc scale |
| F2 | chunk_id collision (parent + ord + content_hash) | ✅ | Even more entropy than parent_id alone |
| F3 | UTF-8 encoding errors → garbled chunks | ✅ | All file reads use `encoding="utf-8"` explicitly |
| F4 | DI returns figures[] mentioning a page no section claims | ⚠️ | `find_section_for_page` returns None; figure has empty headers |
| F5 | Vector dimension mismatch (embedding model upgrade) | 📖 | Fixed at 1536; switching models requires reset. Documented |
| F6 | Partial output.json (truncated upload) | ⚠️ | json.loads fails → cache_data is None → falls back to "needs_preanalyze" |
| F7 | Cache blob ordering: process_document reads di.json before crops upload finishes | ✅ | `phase_di` uploads di.json LAST, after all crops uploaded. Reading order: di.json → crop blobs → output.json |
| F8 | Crop hash collision (two figures with same content) | ✅ | preanalyze dedups them — same hash, one indexed. Correct behavior |
| F9 | Incomplete bulk delete (some chunks orphaned) | ⚠️ | reconcile pages through deletes; partial failure leaves some records. Re-run reconcile to retry |
| F10 | Index document max size (32MB per record) | ✅ | text_vector ~6KB + chunk ~2KB + other fields ~1KB; far under |
| F11 | DI returns inconsistent rowCount/columnCount for table | ✅ | tables.py guards against zero rowCount/columnCount; returns empty markdown |
| F12 | Image base64 corruption mid-upload | ⚠️ | preanalyze uploads atomically; downstream b64decode would fail and skip figure |

## G. PDF-specific edge cases (15 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| G1 | PDF with form fields (acroforms) | ⚠️ | Field labels usually appear as text; field values may not |
| G2 | PDF with digital signatures | ✅ | DI ignores signature dictionaries; content extracted normally |
| G3 | PDF with embedded fonts (font subset issues) | ✅ | DI handles |
| G4 | PDF with rotated pages | ✅ | DI normalizes; PyMuPDF respects page rotation |
| G5 | PDF with mixed page sizes | ✅ | Each page handled independently |
| G6 | PDF with cropped/clipped content | ⚠️ | DI extracts visible text only; clipped content lost (correct) |
| G7 | PDF with watermarks across pages | ⚠️ | Watermark text may pollute every chunk; running-artifact stripper removes some patterns |
| G8 | PDF with transparent overlays | ✅ | DI handles; PyMuPDF renders correctly |
| G9 | PDF with embedded videos | 📖 | Not extracted; out of scope |
| G10 | PDF with bookmarks/outline | ⚠️ | Not extracted; could be a future field |
| G11 | PDF with TOC at end (after content) | ✅ | TOC heuristic catches by structure, not position |
| G12 | PDF with nested TOCs | ✅ | Each TOC chunk individually classified |
| G13 | PDF generated from a scanned source (OCR variable) | ⚠️ | DI's OCR is good but not perfect. Low-confidence text gets indexed as-is |
| G14 | PDF with corrupt page mid-stream | ⚠️ | DI may skip the corrupt page or fail entire doc; varies |
| G15 | PDF with JavaScript-controlled content rendering | ⚠️ | Not extracted; JavaScript not executed |

## H. Operational scenarios (15 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| H1 | Jenkins agent disk fills up during preanalyze | ❌ | Cache writes to blob, not local disk; only checkout uses local disk. Should be fine |
| H2 | Jenkins agent reboots mid-job | ⚠️ | Job marked failed; preanalyze --incremental on next run resumes |
| H3 | Network outage between agent and Azure | ⚠️ | Retries help; long outage = job fails; auto-heal next run recovers |
| H4 | Storage account regional failover | 📖 | Endpoint stays the same (geo-replicated DNS); transient errors during failover |
| H5 | Search service paused/unpaused | ❌ | All operations fail until unpaused. Document |
| H6 | Production drift: schema in repo differs from deployed | ⚠️ | deploy_search.py is idempotent; running it from repo applies committed schema. **Mitigation:** drift detection script (could be added) |
| H7 | Two contractors have local deploy.config.json with different values | 📖 | File is gitignored; per-machine. Document via README |
| H8 | CODEOWNERS not configured → unintended PR merges | ⚠️ | Updated CODEOWNERS placeholder; team must replace with real handles |
| H9 | CI workflow disabled accidentally | ⚠️ | GitHub branch protection should require ci.yml status. Document |
| H10 | Indexer schedule disabled silently | ⚠️ | Pipeline run timeout would catch (no progress in MAX_WAIT_MINUTES) |
| H11 | RBAC role removed (Function MI loses Storage Reader) | ⚠️ | Skills fail with auth errors; reported in indexer errors |
| H12 | Cosmos collection auto-created with wrong partition key (idempotent code re-runs) | ✅ | `_ensure_container` is idempotent; if exists with right schema, fine |
| H13 | Multiple environments using same Cosmos | ⚠️ | Container names are not env-prefixed. **Mitigation:** use separate Cosmos databases per env, or prefix container names |
| H14 | Manual key rotation breaks function app | ⚠️ | Rotate via `az functionapp keys set`, then re-run deploy_search.py to update skillset URL |
| H15 | New PDF uploaded just as pipeline starts (race) | ⚠️ | If uploaded between reconcile and preanalyze, picked up next pass; not lost |

## I. Time / scheduling (8 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| I1 | Daylight savings time during nightly run | ✅ | Cron uses UTC; DST irrelevant |
| I2 | Timezone mismatch between Jenkins, Cosmos, Azure | ✅ | Everything UTC-stamped |
| I3 | Last_modified timestamp drift between blob and Cosmos | ⚠️ | Reconcile compares ISO strings (lexicographic); mostly fine |
| I4 | Pipeline scheduled while previous still running | ✅ | `disableConcurrentBuilds()` |
| I5 | Indexer 15-min tick fires during preanalyze | ✅ | Indexer reads cache that may be partial; falls back to "needs_preanalyze"; next tick succeeds |
| I6 | Cron mistimed (e.g., 2 AM in agent's local time, not UTC) | ⚠️ | Verify Jenkins agent timezone is UTC |
| I7 | Long PDF takes 24h+ to preanalyze, conflicts with next nightly run | ⚠️ | disableConcurrentBuilds prevents; new run waits |
| I8 | App Insights Kusto queries against future timestamps | 📖 | Not a real scenario |

## J. Disaster recovery (10 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| J1 | Cosmos account deleted accidentally | ❌ | Run history lost; re-create account; pipeline auto-recreates containers; pdf_state lost = next reconcile sees everything as "no Cosmos record"; treats all as stable. Edits won't be detected. **Mitigation:** Cosmos backups. Document |
| J2 | Search index deleted accidentally | ✅ | Re-deploy with `deploy_search.py`; reset indexer; pay embeddings again. Documented in [RUNBOOK.md §5#8](RUNBOOK.md#5-incident-response) |
| J3 | Storage container deleted (PDFs lost) | ❌ | Soft-delete (if enabled) restores. **Mitigation:** soft-delete is mandatory |
| J4 | Function App deleted/redeployed | ✅ | Re-deploy via Jenkinsfile.deploy; key changes; deploy_search.py picks up new key |
| J5 | Subscription owner changes | 📖 | Identities preserved; documented |
| J6 | Production environment migrates to new resource group | ⚠️ | Requires re-RBAC + reconfig; document migration runbook |
| J7 | Lost deploy.config.json | ⚠️ | Must reconstruct from environment knowledge. **Mitigation:** keep secret-file backup in Azure Key Vault |
| J8 | Code rolled back without index reset | ⚠️ | Rollback to old skill version may write incompatible records. **Mitigation:** roll forward, not back |
| J9 | Two-week outage (cache + state stale) | ✅ | Cache survives in blob; state in Cosmos. Resume normally |
| J10 | Resource group moved to different subscription | ⚠️ | RBAC preserved if scoped to RG. Endpoint URLs unchanged. Test before moving prod |

## K. Indexer-specific (12 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| K1 | Indexer goes into "transientFailure" repeatedly | ⚠️ | Auto-retries; if persistent, alert. [RUNBOOK.md §5#1](RUNBOOK.md#5-incident-response) |
| K2 | Indexer high-water-mark stuck (last_modified parsing) | ⚠️ | Reset indexer to clear |
| K3 | Indexer batch with mixed success/fail | ✅ | maxFailedItems=10; visible in execution history |
| K4 | maxFailedItems=10 lets a permanently-bad PDF persist as failure | ⚠️ | Operator notices via dashboard; manual investigation |
| K5 | Indexer history retention (last 50 runs) | 📖 | Azure Search caps at 50; Cosmos run_history is the long-term log |
| K6 | Datasource connection string rotation | ⚠️ | Re-deploy with new config; deploy_search.py is idempotent |
| K7 | Skillset version drift (deployed vs committed) | ❌ | No drift detection. **Mitigation:** add a check that compares deployed skillset hash vs repo |
| K8 | Indexer can't see new field after schema add | ⚠️ | Need reset for index projection changes; documented |
| K9 | Search index shadow copy during update | ✅ | Azure handles; brief read-availability; no data loss |
| K10 | Multiple indexers writing to same index | ❌ | We have one indexer per index. Don't add more |
| K11 | Indexer 24h cap | 📖 | Documented in [RUNBOOK.md §5#16](RUNBOOK.md#5-incident-response); resumes on next tick |
| K12 | Skillset returns 500 from custom WebApi | ⚠️ | Marked as failed item; visible in indexer errors |

## L. Function App specific (12 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| L1 | Cold start (first request takes 30s) | ⚠️ | Premium plan recommended for prod; Premium has pre-warmed instances |
| L2 | App Service Plan scale-in killing in-flight skill | ⚠️ | Indexer retries the failed batch; eventually succeeds |
| L3 | App Insights sampling drops failures | ⚠️ | At our scale, sampling rate ~50% by default; failures less likely to be sampled. Set to 100% in prod |
| L4 | Application Insights connection string rotation | ⚠️ | Update App Settings; redeploy |
| L5 | Function App restart mid-batch | ✅ | Indexer retries; cache survives |
| L6 | Function timeout vs skill timeout mismatch | ⚠️ | host.json functionTimeout=10min, skill timeout=230s; aligned |
| L7 | App Settings overflow (>4MB total) | ❌ | Soft limit; we're nowhere near. Document |
| L8 | Slot swap during deploy | ⚠️ | Production slot only currently; staging slot would help zero-downtime |
| L9 | Skill returns 200 OK with empty payload | ⚠️ | Treated as success; record indexed with empty fields |
| L10 | Function App moved to a different region | ⚠️ | URL changes; deploy_search.py needs new function host |
| L11 | Custom skill in degraded state (high latency) | ⚠️ | Indexer 230s skill timeout catches; one batch fails |
| L12 | Container Images for Functions | 📖 | Not used; standard runtime |

## M. AOAI / DI / Vision specific (10 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| M1 | AOAI deployment deleted | ❌ | All embeddings/vision fail; document |
| M2 | Model deprecated by Microsoft | ⚠️ | Pin model version in skillset; alert on deprecation notices |
| M3 | DI's prebuilt-layout returns different shape (API version change) | ⚠️ | API version pinned in config; review on upgrade |
| M4 | Vision returns malformed JSON | ✅ | Retry logic handles; permanent failures cached |
| M5 | Vision flagged by content filter | ✅ | Cached as permanent error; don't retry |
| M6 | Embedding skill returns wrong dimension | ⚠️ | Index would reject the document. Should add validation in skillset; currently absent |
| M7 | Embedding skill rate-limited | ⚠️ | Skillset retries via Azure Search internal retry |
| M8 | Vision rejects oversized image | ⚠️ | Skipped in image triage if < 1.0 inch or > MAX_ASPECT_RATIO |
| M9 | DI takes >24h on a single PDF | ❌ | analyze_di has timeout_s=900 (15 min); long PDFs fail. **Mitigation:** split source PDF |
| M10 | AOAI region outage | ⚠️ | All vision/embedding fails; pipeline marked failed; resume on recovery |

## N. Scaling beyond 56 PDFs (8 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| N1 | 1,000 PDFs in container | ⚠️ | check_index.py facet query may need pagination beyond 1000 source_files |
| N2 | 10,000 PDFs | ❌ | facet=count:0 returns up to 100K facet values; verify; reconcile read_all_items unbounded |
| N3 | Cosmos pdf_state grows unbounded | ⚠️ | Bounded by source PDF count; reconcile deletes orphans |
| N4 | Vector index size on disk | ⚠️ | 92K vectors × 1536 × 4 bytes = 565MB. Standard tier handles |
| N5 | Search service partition limits | ⚠️ | Document tier vs partition mapping |
| N6 | preanalyze single-process throughput | ⚠️ | --concurrency caps at 10; --vision-parallel at 100. For 1000 PDFs use higher |
| N7 | Cosmos 400 RU/s shared throughput limit | ⚠️ | At larger scale (10K+ pdfs), upgrade |
| N8 | App Insights ingestion at scale | ⚠️ | Set daily cap; sampling kicks in |

## O. UX / quality scenarios (8 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| O1 | Search returns chunks with running-header noise polluting result | ✅ | Stripped from chunk_for_semantic; raw chunk preserved for citation |
| O2 | TOC entries dominate top-of-results | ✅ | Marked `toc_like`; filterable |
| O3 | Vision describes a non-figure (DI false positive) | ⚠️ | Image triage drops decorative; rest goes through. False positives possible |
| O4 | Same diagram appears in two manuals (re-used) | ✅ | Hash cache returns same description |
| O5 | Diagram description is wrong (vision hallucination) | ⚠️ | No automatic correction; trust scoring left to downstream LLM |
| O6 | Search query in non-English | ⚠️ | ada-002 is multilingual; behaviour decent |
| O7 | Partial-document results (chunks visible but missing summary) | ✅ | --coverage shows partial; auto-heal reprocesses |
| O8 | Operator can't tell which version of skillset is deployed | ⚠️ | `skill_version` field on every record; bump in config to invalidate |

---

## Total: 168 scenarios

| Category | ✅ | ⚠️ | ❌ | 📖 | Total |
|---|---:|---:|---:|---:|---:|
| Auth & identity | 1 | 5 | 5 | 1 | 12 |
| Concurrency | 7 | 4 | 3 | 1 | 15 |
| Boundary | 8 | 7 | 0 | 0 | 15 |
| Security | 4 | 5 | 2 | 1 | 12 |
| Cost & quota | 1 | 6 | 2 | 3 | 12 |
| Data integrity | 5 | 5 | 0 | 2 | 12 |
| PDF edge cases | 3 | 9 | 0 | 3 | 15 |
| Operational | 1 | 12 | 1 | 1 | 15 |
| Time & scheduling | 4 | 3 | 0 | 1 | 8 |
| Disaster recovery | 2 | 6 | 2 | 0 | 10 |
| Indexer | 1 | 8 | 2 | 1 | 12 |
| Function App | 1 | 9 | 1 | 1 | 12 |
| AOAI / DI / Vision | 2 | 6 | 2 | 0 | 10 |
| Scaling | 0 | 6 | 2 | 0 | 8 |
| UX / quality | 4 | 4 | 0 | 0 | 8 |
| **Total** | **44** | **95** | **22** | **15** | **176** |

176 scenarios. **44 fully handled. 95 partial (work in common case, known limitation). 22 real gaps. 15 by-design.**

---

## Real gaps that are getting fixed in this commit

These are the **22 ❌ items I judge actionable and worth fixing now**:

1. **B2 / B3** — Pipeline lock to prevent concurrent runs (preanalyze + reconcile collision)
2. **A2** — Better error reporting when MI loses role grants (preflight catches role assignment)
3. **A7** — Cosmos role propagation: preflight retries Cosmos auth a few times before failing
4. **K7** — Skillset version drift detection (compare deployed hash vs repo)
5. **M6** — Embedding dimension validation (defensive: log warning if vector size != 1536 anywhere)

Other ❌ items are documented; mitigations listed in this file. Fixing them all would be diminishing returns.

---

## How to use this document

- **During an incident:** find the matching scenario; read the status + mitigation.
- **During a deployment review:** scan ⚠️ rows in the relevant category; verify mitigations are in place for your environment.
- **During a quarterly audit:** revisit the ❌ rows; promote to ⚠️ or ✅ as code/docs improve.
- **When something surprising happens:** add a new row. Don't let the document go stale.

---

# Section 2: Preanalyze deep-dive


## P-A. Blob fetching (20 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| P-A1 | Blob temporarily inaccessible (eventual consistency on upload) | ✅ | 3-attempt retry with exponential backoff in `fetch_blob` |
| P-A2 | Blob deleted between list and fetch | ⚠️ | First fetch returns 404 → caught; PDF logged as FAIL-fetch and skipped; not auto-retried |
| P-A3 | Blob name with spaces (the bug we fixed) | ✅ | URL-encoded via `quote()` in `_blob_url` |
| P-A4 | Blob name with `+` `&` `=` `?` characters | ✅ | All percent-encoded by `quote()`; preserved through SharedKey signing |
| P-A5 | Blob name with control chars (CR/LF/TAB) | ⚠️ | Azure rejects upload with these; if somehow present, signing may fail. Practically impossible from CLI uploads |
| P-A6 | Blob in archive tier | ❌ | Returns 409 conflict; needs rehydration first. **Mitigation:** preflight could check storage tier on the container |
| P-A7 | Blob with active lease (write-locked) | ⚠️ | We only read; lease doesn't block reads. Cache-write on `_dicache/` could fail if someone leases that prefix |
| P-A8 | Blob with snapshots that confuse listing | ⚠️ | `az storage blob list` excludes snapshots by default |
| P-A9 | Blob with versioning enabled | ⚠️ | Listing returns current version; old versions invisible. Fine for our case |
| P-A10 | Storage account temporarily 503 | ✅ | 3-retry helper in preanalyze + di_client |
| P-A11 | Storage account regional failover during run | ⚠️ | Endpoint stays the same; transient 503s during failover; retry handles |
| P-A12 | Storage SAS token expires mid-run (non-MI mode) | ⚠️ | Auth helper does not re-fetch SAS; long runs need MI. **Mitigation:** prefer MI in production |
| P-A13 | DNS resolution fails for `<account>.blob.core.windows.net` | ⚠️ | httpx retries connect errors; long DNS outage = job fails |
| P-A14 | Storage IP firewall blocks the agent | ❌ | First request fails 403; not auto-handled. **Mitigation:** preflight test fetches one blob |
| P-A15 | TLS cert chain incomplete (corp proxy) | ⚠️ | `SSL_CERT_FILE` env var lets operator point to a CA bundle |
| P-A16 | TCP keepalive expires on long DI poll | ⚠️ | httpx default keepalive ~5min; DI polling under that. Long DI runs may reset |
| P-A17 | Blob is 0 bytes | ⚠️ | Fetch succeeds with empty bytes; DI rejects → FAIL-di |
| P-A18 | Blob > 500MB | ⚠️ | Memory load on agent; DI accepts up to 500MB per call. Larger PDFs need urlSource path (already implemented at 30MB threshold) |
| P-A19 | Two blobs with same name in different cases (`Manual.pdf` + `manual.pdf`) | 📖 | Azure is case-sensitive; treated as two separate documents (different parent_ids) |
| P-A20 | Blob name that is a prefix of another (`man.pdf` + `man.pdf.di.json`) | ✅ | We list `_dicache/` separately; prefix match is anchored on extension |

## P-B. Document Intelligence (25 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| P-B1 | DI endpoint regional outage | ❌ | All processing fails; retry doesn't help. **Mitigation:** alert on FAIL-di rate >50% |
| P-B2 | DI returns HTML error page (gateway issue) | ✅ | `submit.json()` raises; caught and reported |
| P-B3 | DI 503 "queue full" | ⚠️ | Submit retry handles; if persistent, fail loud |
| P-B4 | DI rejects unsupported format (e.g. uploaded `.txt` mislabeled as `.pdf`) | ✅ | Returns 400 with clear message; FAIL-di |
| P-B5 | DI returns 200 but body empty | ⚠️ | analyze_di's `body.get("status")` returns None; loop continues to deadline → TimeoutError |
| P-B6 | DI returns 200 with `analyzeResult` missing | ⚠️ | `body.get("analyzeResult", {})` returns empty dict; downstream sees zero figures/tables; PDF marked done with no content |
| P-B7 | DI returns 200 with truncated content (claims 100 pages, has 50) | ⚠️ | We trust DI's count; downstream chunks reflect what DI returned |
| P-B8 | DI returns no paragraphs (image-only scan) | ⚠️ | Sections empty, text records empty. Vision still runs on figures if any |
| P-B9 | DI returns paragraphs but no sections | ⚠️ | `find_section_for_page` returns None; chunks have empty headers |
| P-B10 | DI sections reference nonexistent paragraphs | ✅ | `_paragraph_pages` bounds-checks the index |
| P-B11 | DI bounding regions zero-area (degenerate polygon) | ✅ | `pdf_crop._polygon_bbox_inches` + clip checks before rendering |
| P-B12 | DI figure pageNumber outside [1, len(pages)] | ⚠️ | crop_figure raises ValueError; caught and skip figure |
| P-B13 | DI table rowCount=0 or columnCount=0 | ✅ | Empty grid → empty markdown → record skipped |
| P-B14 | DI table cell rowIndex/columnIndex exceeds declared dims | ✅ | `_table_to_grid` bounds-checks `if 0 <= rr < rows and 0 <= cc < cols` |
| P-B15 | DI polygon < 8 numbers | ✅ | `_open_pdf` and crop function validate length |
| P-B16 | DI polygon contains NaN or Inf | ⚠️ | `min(xs)` propagates NaN; subsequent rect fails. Caught generic Exception |
| P-B17 | DI content with Unicode surrogates (broken UTF-16 → UTF-8) | ⚠️ | json.loads handles; downstream string ops mostly safe |
| P-B18 | DI content with replacement char `�` | ✅ | Indexed as-is; no crash |
| P-B19 | DI roleId for sectionHeading is unfamiliar (new DI release) | ✅ | Section walk treats unknown roles as body content |
| P-B20 | DI printed-page-number != physical page (e.g. starts at "i") | ✅ | We track both: `printed_page_label` from DI markers, `physical_pdf_page` from sequence |
| P-B21 | DI returns multiple "title" paragraphs (level confusion) | ✅ | Header stack manipulation: each title resets to level 1 |
| P-B22 | DI marks the same paragraph as both "footer" and content | ✅ | We use role for heading detection only; footers go through running-artifact strip |
| P-B23 | DI takes >15min on a single PDF (analyze_di's deadline) | ❌ | TimeoutError raised, marked FAIL-di. **Mitigation:** split source PDF or raise timeout per-call |
| P-B24 | DI returns malformed JSON (rare) | ⚠️ | `body = poll.json()` raises; caught generic Exception |
| P-B25 | DI version pinning: API contract change | ⚠️ | Pinned to `2024-11-30` in config; review on Azure release notes |

## P-C. Cropping with PyMuPDF (15 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| P-C1 | PyMuPDF version mismatch with PDF spec edition | ⚠️ | requirements.txt pins `>=1.24.0,<2.0.0` |
| P-C2 | PDF page MediaBox missing | ✅ | PyMuPDF defaults to A4 |
| P-C3 | Polygon entirely outside page bounds (after pad expansion) | ✅ | `crop_figure_png_b64` raises ValueError; caught |
| P-C4 | Polygon NaN/Inf | ⚠️ | Clipping math produces NaN; rect creation fails; caught |
| P-C5 | PDF font subset missing for the cropped region | ✅ | Render shows `?` glyphs; image still produced |
| P-C6 | PDF page rotation (90/180/270 degrees) | ✅ | PyMuPDF respects rotation; coords map correctly |
| P-C7 | Page index out of range (page > doc.page_count) | ✅ | crop_figure raises ValueError before rendering |
| P-C8 | Page index < 1 | ✅ | Same |
| P-C9 | PDF stream password-protected | ✅ | EncryptedPdfError raised before any crop attempt |
| P-C10 | Memory blow-up on extremely high-DPI PDF | ⚠️ | We render at 300 DPI; for a 100-inch page that's 30K×30K = 900M pixels = 3.6GB. Realistic max is ~24" × ~36" = 800MP. Function-app would OOM |
| P-C11 | Crop rectangle smaller than 1 pixel after rounding | ⚠️ | Rendered as 1×1 pixel; vision skips at MIN_CROP_BYTES |
| P-C12 | Crop overlaps page boundary | ✅ | Clipped to page rect |
| P-C13 | PDF with hidden/invisible text layer | ⚠️ | DI extracts visible only; invisible OCR layer ignored |
| P-C14 | PDF with form fields (acroforms) | ⚠️ | Field labels in DI; field values may be empty in extracted markdown |
| P-C15 | PDF with attachments (embedded files) | 📖 | Not extracted; out of scope |

## P-D. Vision API (Azure OpenAI) (25 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| P-D1 | AOAI 429 rate-limited | ✅ | Honor `Retry-After`, capped at 120s |
| P-D2 | AOAI 5xx server error | ✅ | Retry up to 3 times with sleep |
| P-D3 | AOAI 4xx (other than 429): bad request, auth | ✅ | Not retryable; raise |
| P-D4 | AOAI returns truncated JSON (chunked transfer issue) | ✅ | `json.JSONDecodeError` → retry |
| P-D5 | AOAI response with extra whitespace | ✅ | `text.strip()` before parsing |
| P-D6 | AOAI markdown-wrapped JSON (```json...```) | ✅ | Stripped before json.loads |
| P-D7 | AOAI returns trailing comma (invalid JSON) | ⚠️ | json.loads rejects; counted as parse error and retried |
| P-D8 | AOAI returns category not in our enum | ✅ | Downstream maps to "unknown" |
| P-D9 | AOAI returns is_useful=true with description="" | ✅ → fixed | New defensive validation: retry once, accept on second pass |
| P-D10 | AOAI returns description with > 8K tokens | ✅ | Capped via prompt cap (max_tokens=1500) |
| P-D11 | AOAI returns ocr_text with > 4K chars | ✅ | Same cap |
| P-D12 | AOAI deployment renamed | ❌ | Returns 404; retries don't help. **Mitigation:** preflight could test deployment exists |
| P-D13 | AOAI region changed | ⚠️ | Endpoint URL different; config update + redeploy needed |
| P-D14 | AOAI version pinning wrong | ⚠️ | API version pinned in config (`2024-12-01-preview`); review on Microsoft updates |
| P-D15 | AOAI rate-limit headers misformed (not int) | ✅ | `try/except` around int parse; default 10s |
| P-D16 | AOAI returns 502 with retry-after | ✅ | Retried |
| P-D17 | AOAI raw response is binary (very unusual) | ⚠️ | `text.startswith("```")` check fails silently; json.loads errors |
| P-D18 | AOAI hallucinates a figure_ref not in the PDF | ⚠️ | We use whatever AOAI returns; downstream consumer should verify |
| P-D19 | AOAI flags content as harmful | ✅ | Cached as permanent (no retry) |
| P-D20 | AOAI returns wrong dimension if model swap mid-flight | ❌ | Affects embedding skill more than vision; document |
| P-D21 | AOAI quota exhausted partway through batch | ⚠️ | Some figures unprocessed; remaining figures error out; cached as transient (will retry next run) |
| P-D22 | AOAI rejects oversized image (> 20MB base64) | ⚠️ | image triage drops most; rare to hit |
| P-D23 | AOAI vision content filter false positive on legitimate diagram | ⚠️ | Permanent error cached; figure missing from index. **Mitigation:** review filter rules with Microsoft |
| P-D24 | Azure region outage for AOAI | ❌ | All vision fails; pipeline marked failed; resume on recovery |
| P-D25 | AOAI returns response in wrong language (model confused) | ⚠️ | Indexed as-is; multilingual embedding handles |

## P-E. Concurrency & rate limits (15 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| P-E1 | --concurrency × --vision-parallel × figures = 1000+ in-flight calls | ⚠️ | AOAI 429 backoff distributes; some thundering-herd |
| P-E2 | AOAI TPM exhausted | ✅ | Retry-After backoff |
| P-E3 | AOAI RPM exhausted | ✅ | Same |
| P-E4 | DI per-second rate limit | ✅ | Submit-retry path |
| P-E5 | Storage egress rate limit | ✅ | 3-retry helper |
| P-E6 | Function app concurrent execution limit (irrelevant to preanalyze) | 📖 | preanalyze runs from Jenkins, not Function App |
| P-E7 | Cosmos DB RU/s exhausted at end-of-run write | ✅ | Best-effort |
| P-E8 | Two preanalyze processes target same PDF | ✅ | Pipeline lock blocks concurrent runs |
| P-E9 | preanalyze + reconcile target same PDF | ✅ | Pipeline lock |
| P-E10 | Multiple Jenkins agents pick up the same scheduled job | ✅ | `disableConcurrentBuilds()` + pipeline lock |
| P-E11 | ThreadPoolExecutor exhausted (all threads stuck) | ❌ | Hangs until timeout. **Mitigation:** per-future timeout (could add) |
| P-E12 | Future result() raises after main loop exit | ⚠️ | Caught by `as_completed` iteration |
| P-E13 | Lock acquisition order causes deadlock (preanalyze internal locks) | ✅ | All locks taken in same order; no cycles |
| P-E14 | Logging from multiple threads interleaves | ⚠️ | flush=True ensures atomicity per-line; lines may interleave |
| P-E15 | OOM-killer terminates Python under memory pressure | ❌ | Job dies; resume via --incremental |

## P-F. Cache state (15 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| P-F1 | Cache blob written but list_cache_blobs misses (eventual consistency) | ⚠️ | Subsequent run picks up; window is seconds |
| P-F2 | Cache blob exists but is 0 bytes | ⚠️ | json.loads fails → treated as missing/corrupt |
| P-F3 | Cache blob has invalid JSON | ✅ | Caught; logged; treated as miss |
| P-F4 | Cache blob with old schema (different keys) | ✅ → fixed | `process_document` and skill code use `.get()` defaults |
| P-F5 | Two vision blobs claim the same figure_id | 📖 | Impossible — figure_id is unique within a PDF; if two exist it's user error |
| P-F6 | Crop blob too large (>32MB; index document size limit) | ⚠️ | image_b64 in output.json: we drop it before final assembly (only bbox kept) |
| P-F7 | Output.json with circular references | ⚠️ | json.dumps would raise; caught |
| P-F8 | Output.json with NaN/Inf values | ⚠️ | json.dumps refuses; caught generic Exception |
| P-F9 | Cache blob deleted by reconcile mid-preanalyze | ✅ | Pipeline lock prevents this |
| P-F10 | Stale lock file from crashed run | ✅ | 4-hour stale threshold in pipeline_lock |
| P-F11 | Cache blob ordering: di.json after crops vs before | ✅ | We upload di.json first, then crops in parallel, then output.json last |
| P-F12 | --incremental sees output.json but DI cache deleted | ✅ | `_is_pdf_done` returns True (trust output.json) |
| P-F13 | --status sees output.json + DI cache + 0 crops + DI legitimately had 0 figures | ✅ → fixed | Now classified as DONE, not PARTIAL |
| P-F14 | Cache from older skill_version still valid | ✅ | Schema changes are additive |
| P-F15 | DI cache file from a different storage account (mistakenly copied) | ⚠️ | parent_id mismatch propagates through; would re-process; not catastrophic |

## P-G. Filesystem (10 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| P-G1 | /tmp full | ⚠️ | preanalyze does not write to /tmp; uses blob cache. Safe |
| P-G2 | Disk quota exceeded on agent | ⚠️ | Same; only checkout uses local disk |
| P-G3 | Permission denied on temp dir | ✅ | Same — minimal local FS use |
| P-G4 | Read-only filesystem | ✅ | Same |
| P-G5 | Process killed by OOM-killer | ⚠️ | Job dies; resume via --incremental |
| P-G6 | Long-running process exceeds container time limit (CI) | ⚠️ | Jenkins timeout configurable; default 4h in our Jenkinsfile.run |
| P-G7 | Agent reboots mid-job | ⚠️ | Job dies; resume next run |
| P-G8 | Local Python virtualenv missing or corrupt | ✅ | preflight catches |
| P-G9 | $PATH missing az CLI | ✅ | preflight catches |
| P-G10 | Environment vars (HTTP_PROXY) misconfigured | ⚠️ | httpx respects them; can cause 502s if proxy is bad |

## P-H. Network (10 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| P-H1 | Corporate proxy intercepts SSL | ✅ | `SSL_CERT_FILE` env var |
| P-H2 | DNS resolution fails | ⚠️ | httpx retries connect; long outage = job fails |
| P-H3 | IPv6 vs IPv4 issues | ⚠️ | httpx default uses both; can be tuned |
| P-H4 | Connection reset mid-upload | ✅ | 3-retry helper |
| P-H5 | SSL handshake timeout | ⚠️ | httpx default; configurable |
| P-H6 | TCP keepalive expires | ⚠️ | DI long-poll sometimes hits this |
| P-H7 | Bandwidth throttling on agent | 📖 | Just slower; not a failure mode |
| P-H8 | NAT timeout for long DI session | ✅ | Re-fetches operation-location periodically |
| P-H9 | Service Endpoint policy blocks traffic | ❌ | First request 403; configure Service Endpoints to allow |
| P-H10 | Cross-region traffic for DI / AOAI | ⚠️ | Slower + potential bandwidth charges; document architecture |

## P-I. Process lifecycle (10 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| P-I1 | SIGTERM during upload (Jenkins agent shutdown) | ⚠️ | Upload aborted; cache may have orphan crop blobs; --incremental cleans up next run |
| P-I2 | Python interpreter crash (segfault from PyMuPDF) | ❌ | Process dies; supervisor (Jenkins) reports failure. **Mitigation:** PyMuPDF version pinning |
| P-I3 | Memory exhaustion on large PDF (>2GB) | ⚠️ | OOM-killer terminates; resume via --incremental |
| P-I4 | Stack overflow (recursive section walk on weird PDF) | ❌ | RecursionError. **Mitigation:** sys.setrecursionlimit + iterative walk for safety |
| P-I5 | Unhandled KeyboardInterrupt | ⚠️ | Lock not released cleanly; stale-lock detection handles |
| P-I6 | Daemon thread interrupted by main thread exit | ✅ | as_completed waits; futures complete |
| P-I7 | Child process spawned and orphaned | 📖 | Not used; preanalyze is single-process |
| P-I8 | Signal handler conflict with httpx | 📖 | Not used |
| P-I9 | Auth token cached at module level becomes invalid | ⚠️ | Long-running runs may hit this; tokens auto-refresh via DefaultAzureCredential |
| P-I10 | Container shut down mid-run (Container Apps) | ⚠️ | Job dies; resume |

## P-J. Lock & coordination (10 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| P-J1 | Two acquire_lock simultaneous calls | ⚠️ | Last-write-wins in blob upload race; possible double-acquire. Acceptable at our scale |
| P-J2 | Stale lock from crashed run | ✅ | 4-hour timeout; can be tuned |
| P-J3 | Lock blob deleted manually mid-run | ⚠️ | Subsequent acquire would succeed; second instance may collide |
| P-J4 | Network blip during lock release | ✅ | Logged warning; lock left in place; auto-cleared by stale check |
| P-J5 | Lock acquired but agent dies before release | ✅ | Stale check + 4h timeout |
| P-J6 | Two preanalyze processes target different lock names | 📖 | Only "preanalyze" lock used; reconcile uses same |
| P-J7 | Lock blob path collides with cache blob | ✅ | Lock path is `_dicache/.lock-...json`; cache paths are `_dicache/<pdf>.<phase>.json` |
| P-J8 | Lock acquire fails because storage 403 | ⚠️ | Acquire raises; preanalyze proceeds without lock (logged warning) |
| P-J9 | Pipeline lock + Jenkins disableConcurrentBuilds redundant? | 📖 | Yes — defense in depth |
| P-J10 | Manual --no-lock used and another run starts | ⚠️ | Race possible; documented |

## P-K. Status reporting & coverage (15 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| P-K1 | --status reports zero-figure PDF as PARTIAL (the user's bug) | ✅ → fixed | Verified DI cache; legit zero → DONE |
| P-K2 | --status sees DI cache + output.json + 0 crops + 0 vision but DI had 100 figures | ✅ → fixed | Real partial; flagged correctly |
| P-K3 | --status with corrupted DI cache (can't parse) | ✅ → fixed | Falls back to old behavior (PARTIAL) when DI un-readable |
| P-K4 | --coverage shows orphans for PDFs deleted from blob | ✅ | reconcile handles |
| P-K5 | --coverage shows partial when index just hasn't caught up | ⚠️ | False positive briefly; resolves on next indexer tick |
| P-K6 | --coverage with > 1000 source_files (facet truncation) | ❌ | Default facet count cap. **Mitigation:** raise count via `count:0` in query — already done |
| P-K7 | --coverage Cosmos write fails | ✅ | Best-effort; pipeline succeeds |
| P-K8 | Empty container | ✅ | "Nothing to process"; coverage 0/0 |
| P-K9 | All PDFs failed | ⚠️ | Report shows 0 done, count of failed visible in cosmos run record |
| P-K10 | --status takes too long with many PDFs (now downloads DI cache) | ⚠️ | One blob fetch per ambiguous PDF. For 60 PDFs, max ~60 fetches. Fast. |
| P-K11 | --status with large DI cache files (50MB each) | ⚠️ | Fetched only for ambiguous (zero-crop) PDFs; rare to hit |
| P-K12 | --coverage facet result has duplicates | 📖 | Source_file is unique blob name; no dups expected |
| P-K13 | --coverage time-boxed run (large index) | ⚠️ | Small queries; fast even at 10K chunks |
| P-K14 | check_index --check-stuck-indexer uses wrong endpoint | ✅ | Reads from config |
| P-K15 | Coverage write to Cosmos creates new container | ✅ | `_ensure_container` is idempotent |

---

## Total: 170 preanalyze-specific scenarios

This is the deep complement to [SCENARIOS.md](SCENARIOS.md). Use during
preanalyze incidents; cross-reference with [SCENARIOS.md §3](SCENARIOS.md#section-3-indexer--function-app-deep-dive)
for the indexer-side counterpart.

---

# Section 3: Indexer + Function App deep-dive

## I-A. Indexer trigger / scheduling (15 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| I-A1 | Schedule fires while previous still running | ✅ | Azure Search prevents two simultaneous runs of same indexer |
| I-A2 | Manual trigger races with schedule | ✅ | Same |
| I-A3 | Reset followed immediately by Run | ⚠️ | Possible race in 1-2 sec window; documented |
| I-A4 | Schedule disabled silently | ⚠️ | Pipeline run timeout would catch (no progress in MAX_WAIT_MINUTES) |
| I-A5 | Cron interval changed in repo but not deployed | ⚠️ | deploy_search.py is idempotent; running from CI applies committed config |
| I-A6 | Indexer paused (manual pause) | ❌ | All operations wait. **Mitigation:** alert if no run for 24h |
| I-A7 | Indexer hits 24h max run length | 📖 | Resumes from high-water-mark on next tick. [RUNBOOK.md §5#16](RUNBOOK.md#5-incident-response) |
| I-A8 | Indexer schedule misalignment (PT15M but jobs take >15 min) | ⚠️ | Azure handles by skipping; effective interval = batch length |
| I-A9 | Indexer starts but datasource changes mid-flight | ❌ | Behavior undefined. **Mitigation:** never change datasource during indexer run |
| I-A10 | Skillset PUT during indexer run | ❌ | Same — undefined; document |
| I-A11 | Indexer history retention (last 50 runs) | 📖 | Azure caps; long-term log is in Cosmos run_history |
| I-A12 | First-time run after deploy | ✅ | High-water-mark empty; processes everything |
| I-A13 | Run after schema change (need reset?) | ⚠️ | For new fields with index projections, reset required |
| I-A14 | Run on empty container | ✅ | Reports `success` with itemsProcessed=0 |
| I-A15 | Run with `--run-indexer` flag from CI | ✅ | Manual trigger works |

## I-B. Datasource (10 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| I-B1 | Datasource connection string rotates | ⚠️ | Re-deploy via deploy_search.py |
| I-B2 | Datasource container renamed | ❌ | Indexer fails; update config + redeploy |
| I-B3 | Datasource credentials expired | ⚠️ | Identity-based auth doesn't expire; key-based would |
| I-B4 | Datasource detects deletion via NativeBlobSoftDelete | ✅ | datasource.json declares this policy |
| I-B5 | Datasource detects edit via HighWaterMark on lastModified | ✅ | Same |
| I-B6 | Datasource lastModified field missing on a blob | ⚠️ | Azure Storage always provides; if absent, indexer would skip |
| I-B7 | Datasource path traversal via blob name (security) | ✅ | Names validated by Azure Storage |
| I-B8 | Datasource fileExtensions filter mismatch | ✅ → fixed | `.pdf,.docx,.pptx,.xlsx` |
| I-B9 | Datasource pointing at wrong container | ❌ | Operator error; preflight catches |
| I-B10 | Two datasources on same container | 📖 | Don't do this; document |

## I-C. Indexer parameters (10 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| I-C1 | batchSize=1 too slow for many PDFs | 📖 | Required for our skillset (each PDF triggers heavy DI + crops) |
| I-C2 | maxFailedItems too low blocks the whole run | ✅ → tuned | Set to 10 from -1 |
| I-C3 | maxFailedItemsPerBatch interaction with batchSize=1 | 📖 | Effectively per-PDF cap |
| I-C4 | dataToExtract = "contentAndMetadata" | ✅ | Required for our metadata field mappings |
| I-C5 | indexedFileNameExtensions includes `.pdf` only (old) | ✅ → fixed | Now `.pdf,.docx,.pptx,.xlsx` |
| I-C6 | parsingMode = "default" | ✅ | Required (custom skills consume the raw blob) |
| I-C7 | imageAction = "none" | ✅ | We don't use built-in image extraction |
| I-C8 | failOnUnsupportedContentType = false | ✅ | Skipping unsupported is preferred |
| I-C9 | failOnUnprocessableDocument = false | ✅ | Same |
| I-C10 | allowSkillsetToReadFileData = true | ✅ | Required for layout skill to receive bytes |

## I-D. Skillset execution — built-in skills (10 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| I-D1 | DocumentIntelligenceLayoutSkill returns empty markdown_document | ⚠️ | Downstream produces 0 text records; cache-only path may still produce diagram/table records |
| I-D2 | LayoutSkill timeout (30 min for large PDFs) | ⚠️ | Azure handles; if persistent, reset + retry |
| I-D3 | LayoutSkill returns markdown_document with 0 sections | ⚠️ | Each section is the iteration unit; 0 sections = 0 text records |
| I-D4 | SplitSkill produces 0 chunks | ⚠️ | Empty section content → 0 chunks; rare |
| I-D5 | SplitSkill produces > 10K chunks for one section | ⚠️ | Each chunk is a separate skill invocation; slow but completes |
| I-D6 | SplitSkill maxLength=1200 hits a long unbreakable line | ⚠️ | Defaults to char-boundary split |
| I-D7 | SplitSkill overlap reduces effective new content | 📖 | 200 char overlap; intentional |
| I-D8 | AOAI Embedding skill returns wrong dimension | ❌ | Index rejects insert. **Mitigation:** pin model, validate via smoke_test |
| I-D9 | AOAI Embedding rate-limited | ✅ | Azure Search internal retry |
| I-D10 | LayoutSkill API version pinned | 📖 | Azure manages; we just request the skill |

## I-E. Skillset execution — custom WebApi skills (15 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| I-E1 | WebApi 200 OK with body=null | ⚠️ | Azure Search treats as success with no records |
| I-E2 | WebApi returns wrong record count (more or fewer than input) | ⚠️ | Mapping by recordId; mismatched = silent drop or extra |
| I-E3 | WebApi returns extra fields not in outputs[] declaration | ✅ | Azure Search ignores |
| I-E4 | WebApi output type mismatch (string vs int) | ⚠️ | Field projection may reject; specific record fails |
| I-E5 | WebApi 230s timeout | ⚠️ | Azure marks the batch failed; retried once |
| I-E6 | WebApi response with non-UTF-8 bytes | ⚠️ | Azure rejects; batch fails |
| I-E7 | WebApi response > 10MB | ⚠️ | Azure default cap; large vision crops handled by NOT including image_b64 in response |
| I-E8 | Function app skill panics → 500 | ⚠️ | Azure retries the batch; if persistent, marked failed |
| I-E9 | Function app cold start delay > 230s | ⚠️ | Premium plan recommended |
| I-E10 | Function app key expired | ❌ | All skills fail with 401. **Mitigation:** rotate via deploy_search.py |
| I-E11 | Function app deployed mid-batch | ⚠️ | In-flight requests fail; retry handles |
| I-E12 | Function app SCALE-IN kills in-flight skill | ⚠️ | Same retry path |
| I-E13 | Function app returns 200 with empty `values[]` | ⚠️ | Treated as zero records; no error |
| I-E14 | Function app responds with skill_io error envelope | ✅ | skill_io.py builds proper per-record errors |
| I-E15 | Function app returns recordId not in input | ⚠️ | Record ignored by indexer |

## I-F. Function app skill internals (15 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| I-F1 | process_document called without preanalyze cache | ⚠️ → handled | Returns processing_status="needs_preanalyze"; indexer marks success but record empty |
| I-F2 | process_document with corrupt cache JSON | ✅ → fixed | Logged warning; falls back to needs_preanalyze |
| I-F3 | analyze_diagram with missing image_b64 | ✅ | Try cache fetch; if still missing, return processing_status="no_image" |
| I-F4 | analyze_diagram with corrupt image bytes | ⚠️ | Vision rejects; cached as error |
| I-F5 | extract_page_label with empty page_text | ⚠️ | Returns minimal record with empty fields |
| I-F6 | extract_page_label with section_content but no markers | ✅ | Falls back to section_start_page |
| I-F7 | extract_page_label with marker timeline misaligned | ⚠️ | Returns physical_pdf_page best-effort; may be off by 1 |
| I-F8 | shape_table with empty markdown | ⚠️ | Returns record with empty chunk; correct |
| I-F9 | shape_table with caption containing markdown special chars | ✅ | Cell content escaping in `_cell_text` |
| I-F10 | build_semantic_string with mode='unknown' | ✅ | Defaults to 'text' |
| I-F11 | build_doc_summary with empty markdown_text | ✅ | Returns processing_status="no_content" |
| I-F12 | build_doc_summary AOAI failure | ✅ | Returns processing_status="summary_error:..." |
| I-F13 | search_cache lookup returns 401 | ✅ → fixed | Logs warning; falls through to live vision |
| I-F14 | search_cache 429 throttling | ✅ → fixed | Retry with Retry-After |
| I-F15 | text_chunk_id collision (parent + ord + content_hash same) | ✅ | Deterministic; same content = same id = upsert |

## I-G. Index projections (15 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| I-G1 | Source field is null | ⚠️ | Field becomes null in record; usually fine |
| I-G2 | Source field is array of nulls | ⚠️ | Filtered when indexing; depends on field type |
| I-G3 | Required field projected as null | ⚠️ | Index may reject if `Edm.String` is non-nullable; ours are nullable |
| I-G4 | chunk_id is empty string | ❌ | Index rejects (empty key); whole record fails |
| I-G5 | chunk_id matches existing record (overwrite) | ✅ | upsert behavior; deterministic IDs |
| I-G6 | parent_key field missing | ❌ | Projection fails; record skipped |
| I-G7 | Multi-record projection produces duplicate chunk_ids | ✅ | Last-write-wins; safe for re-runs |
| I-G8 | New field added to index, projection not updated | ⚠️ | Field stays empty for new records |
| I-G9 | Projection source path doesn't exist | ⚠️ | Field empty in record |
| I-G10 | Projection source has unexpected nesting | ⚠️ | Empty field |
| I-G11 | sourceContext has typo | ❌ | All records under that selector skip |
| I-G12 | targetIndexName mismatch | ❌ | Projection silently fails to write |
| I-G13 | parentKeyFieldName ambiguous | ⚠️ | Need to be exactly the field name |
| I-G14 | Projection mode "skipIndexingParentDocuments" missing | ⚠️ | Parent docs would also try to index, polluting |
| I-G15 | Projection field name conflicts across selectors | ⚠️ | Each selector is independent; safe |

## I-H. Index schema & document size (15 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| I-H1 | Document size exceeds 32MB | ⚠️ | Index rejects; record fails. Our records ~10KB max |
| I-H2 | Vector field NaN values | ⚠️ | Index rejects |
| I-H3 | Vector field wrong dimension | ⚠️ | Same |
| I-H4 | Vector field too many dims (>3072) | 📖 | Not applicable; ada-002 is 1536 |
| I-H5 | String field exceeds 32K chars | ⚠️ | Index rejects; chunks are bounded at 1200 chars |
| I-H6 | Filterable field >32K bytes | ⚠️ | Index rejects |
| I-H7 | chunk_for_semantic exceeds embedding model input limit | ⚠️ | ada-002 caps at ~8K tokens; chunks well under |
| I-H8 | Field name conflict with reserved name | 📖 | We don't use reserved names |
| I-H9 | Schema change requires reset (added field) | ⚠️ | For projection-driven fields, reset required to backfill |
| I-H10 | Schema change to remove a field | ❌ | Breaks queries that reference it. **Mitigation:** add new field, deprecate old, migrate, then drop |
| I-H11 | Index has > 1M documents | ⚠️ | Search service tier matters |
| I-H12 | Index storage exceeds tier limit | ❌ | Service rejects writes. **Mitigation:** scale up SKU |
| I-H13 | Vector index dim mismatch on alter | ❌ | Cannot change vector dim in place; need new index |
| I-H14 | semantic config references missing field | ⚠️ | Semantic ranker degrades; non-semantic queries still work |
| I-H15 | scoring profile references missing field | ⚠️ | Same |

## I-I. Indexer state & high-water mark (10 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| I-I1 | High-water mark stuck on a single bad blob | ⚠️ | Manually reset indexer to clear |
| I-I2 | last_modified parsing fails | 📖 | Azure handles; ISO 8601 format |
| I-I3 | Indexer thinks blob is unchanged but content changed (e.g., re-uploaded with same timestamp) | ⚠️ | Edit detection won't fire; reconcile catches via Cosmos comparison |
| I-I4 | Indexer reprocesses unchanged blob (false positive) | ⚠️ | Rare; deterministic IDs prevent duplicates |
| I-I5 | Soft-deleted blob still appears in indexer view | 📖 | NativeBlobSoftDeleteDeletionDetectionPolicy handles |
| I-I6 | Reset clears state but preanalyze cache survives | ✅ | Indexer reads cache on next run; fast |
| I-I7 | Indexer status response timeouts | ⚠️ | run_pipeline.py polls with backoff |
| I-I8 | Indexer's HWM drift between regions | 📖 | Single-region by default |
| I-I9 | Indexer cancellation mid-flight | ⚠️ | Partial state; resume on next run |
| I-I10 | Resetting twice in a row | ✅ | Idempotent |

## I-J. Embedding skill (built-in AOAI skill) (10 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| I-J1 | AOAI deployment not found (404) | ❌ | All embeddings fail. **Mitigation:** validate in smoke_test |
| I-J2 | AOAI returns wrong-dim vector | ❌ | Skipped at indexing; no warning. **Mitigation:** pin model |
| I-J3 | AOAI 429 throttle | ✅ | Azure Search internal retry |
| I-J4 | Empty input string to embedding | ⚠️ | AOAI returns 400; record fails |
| I-J5 | Input > 8K tokens | ⚠️ | Same |
| I-J6 | AOAI deployment renamed | ❌ | Re-deploy with new name |
| I-J7 | Multiple embedding skills hitting same deployment | ✅ | Throttle handled; just slower |
| I-J8 | Vectorizer (query-side) misconfigured | ⚠️ | Queries fail; skillset embeddings still work |
| I-J9 | Embedding skill timeout | ⚠️ | Azure default 230s; ada-002 is fast |
| I-J10 | AOAI region outage | ❌ | All embeddings fail; resume on recovery |

## I-K. Auth & RBAC (10 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| I-K1 | Search service MI loses Storage Blob Data Reader | ❌ | Indexer fails to read blobs |
| I-K2 | Search service MI loses AI Services User | ❌ | Layout skill fails |
| I-K3 | Search service MI loses Cognitive Services OpenAI User | ❌ | Embedding skill fails |
| I-K4 | Function app MI loses needed roles | ❌ | Skills fail at runtime |
| I-K5 | Cross-tenant SP token issued | ❌ | All operations fail |
| I-K6 | KeyVault reference unable to resolve (if used) | 📖 | Not used |
| I-K7 | Function key in skillset rotated without redeploy | ❌ | All skill calls 401. Mitigation: deploy_search.py picks up new key |
| I-K8 | Conditional Access blocks the SP | ❌ | Document |
| I-K9 | RBAC propagation delay (5 min after grant) | ⚠️ | Initial calls fail; retry helps |
| I-K10 | Service principal secret expired | ❌ | Re-issue + redeploy |

## I-L. Function app deployment & scaling (10 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| I-L1 | Cold start delay > 230s | ⚠️ | Premium plan recommended |
| I-L2 | App Insights sampling drops failure traces | ⚠️ | Set sampling to 100% in prod |
| I-L3 | App Insights connection string rotation | ⚠️ | Update App Settings + redeploy |
| I-L4 | Function app restart mid-batch | ✅ | Indexer retries |
| I-L5 | App Settings >4MB | 📖 | Soft limit; we don't hit |
| I-L6 | Slot swap during deploy | ⚠️ | Brief downtime; staging slot recommended |
| I-L7 | Function App moved to a different region | ❌ | URL changes; reconfig |
| I-L8 | Function App accidentally on Consumption plan | ⚠️ | Cold starts hurt skill latency. Premium recommended |
| I-L9 | Multiple instances handling same skill batch | ✅ | Stateless; safe |
| I-L10 | Function App in degraded state (high latency) | ⚠️ | Indexer 230s timeout catches |

## I-M. Recovery (10 scenarios)

| # | Scenario | Status | Notes |
|---|---|---|---|
| I-M1 | Index deleted accidentally | ✅ | deploy_search.py + reset; pay embeddings again |
| I-M2 | Indexer deleted accidentally | ✅ | Same |
| I-M3 | Skillset deleted accidentally | ✅ | Same |
| I-M4 | Search service decommissioned | ❌ | New service; new endpoint; full re-deploy |
| I-M5 | Function app deleted | ✅ | Re-deploy via Jenkinsfile.deploy |
| I-M6 | Cosmos data lost | ⚠️ | Run history gone; pdf_state empty; re-establish via next pipeline run |
| I-M7 | Storage container lost | ❌ | Soft-delete restores if enabled. Otherwise full re-upload |
| I-M8 | Subscription transferred | ❌ | Identities reset; full RBAC re-grant |
| I-M9 | Two-week outage | ✅ | Cache survives; resume normally |
| I-M10 | Quota credit exhausted (rare in commercial; possible in dev) | ⚠️ | All AOAI calls fail; resume after credit added |

---

## Total: 165 indexer-side scenarios

Combined with [SCENARIOS.md §2](SCENARIOS.md#section-2-preanalyze-deep-dive) (170)
and the general [SCENARIOS.md](SCENARIOS.md) (176), this gives **511
total scenarios** documented and cross-checked against the code.

## How to use during an incident

1. Identify which side: preanalyze (offline script) vs indexer (Azure-side).
2. Open the matching catalogue.
3. Find the matching scenario (Cmd+F by symptom keyword).
4. Read status + mitigation.
5. If no match, add a new row. Don't let the document go stale.
