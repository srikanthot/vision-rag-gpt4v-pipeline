"""
End-to-end pipeline orchestrator.

One command runs the full operational loop:
  1. reconcile.py        --- detect added/edited/deleted PDFs, purge stale data
  2. preanalyze.py --incremental   --- run DI + Vision for any new/edited PDF
  3. wait for indexer    --- poll status until idle (success or known error)
  4. check_index.py --coverage --write-status   --- report state, persist to Cosmos

This is what Jenkins runs nightly and what an operator runs after
uploading new PDFs. It is safe to run repeatedly; every step is
idempotent.

Usage:
    python scripts/run_pipeline.py --config deploy.config.json
    python scripts/run_pipeline.py --config deploy.config.json --skip-reconcile
    python scripts/run_pipeline.py --config deploy.config.json --max-wait-minutes 60
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cosmos_writer  # noqa: E402

API_VERSION = "2024-05-01-preview"
SEARCH_SCOPE = "https://search.windows.net/.default"

REPO_ROOT = Path(__file__).resolve().parent.parent


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=REPO_ROOT,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


def run_step(label: str, cmd: list[str], allow_fail: bool = False) -> tuple[int, str]:
    """Run a subprocess step and stream its output. Returns (returncode, output)."""
    print()
    print("=" * 70)
    print(f"  STEP: {label}")
    print(f"  CMD:  {' '.join(cmd)}")
    print("=" * 70)
    proc = subprocess.run(cmd, capture_output=False, text=True)
    rc = proc.returncode
    if rc != 0 and not allow_fail:
        print(f"\nSTEP FAILED: {label} (exit {rc})")
    return rc, ""


def wait_for_indexer_idle(endpoint: str, indexer_name: str, max_minutes: int) -> dict:
    """Poll indexer status until it's not 'inProgress'. Returns the
    final lastResult dict (may indicate success, transientFailure, or
    persistentFailure)."""
    url = f"{endpoint}/indexers/{indexer_name}/status?api-version={API_VERSION}"
    cred = DefaultAzureCredential()
    deadline = time.time() + max_minutes * 60
    backoff = 15.0
    last_status = None

    print()
    print("=" * 70)
    print(f"  STEP: wait for indexer {indexer_name} to settle")
    print(f"  Polling every ~{int(backoff)}s, max {max_minutes} min")
    print("=" * 70)

    while time.time() < deadline:
        token = cred.get_token(SEARCH_SCOPE).token
        with httpx.Client(timeout=30.0) as c:
            resp = c.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            print(f"  status fetch returned {resp.status_code}; retrying")
            time.sleep(backoff)
            continue
        body = resp.json()
        last_status = body.get("status") or "unknown"
        last_result = body.get("lastResult") or {}
        last_run_status = last_result.get("status") or "unknown"
        items = last_result.get("itemsProcessed", "?")
        print(f"  indexer={last_status}  lastResult={last_run_status}  items={items}")
        if last_status != "inProgress" and last_run_status != "inProgress":
            return last_result
        time.sleep(backoff)

    print(f"  timeout: indexer still {last_status} after {max_minutes} min")
    return {"status": "timeout", "itemsProcessed": 0}


def _run_heal_passes(cfg: dict, config_path: str, max_passes: int,
                     wait_minutes: int, endpoint: str, indexer_name: str,
                     py: str) -> dict:
    """Audit + repair loop. Each pass:
       1. Read current per-PDF state from Cosmos pdf_state.
       2. Identify PDFs with status in {partial, not_started, failed}.
       3. For each, delete its cache blobs (forces fresh DI on next preanalyze).
       4. Run preanalyze --incremental (which will pick up the now-empty cache).
       5. Wait for the indexer to drain.
       6. Re-run check_index --coverage --write-status to refresh state.
       Stop when a pass finds nothing left to heal, or max_passes reached.

       Returns a dict of per-pass stats."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import cosmos_writer  # noqa: WPS433
    from preanalyze import delete_blob, list_cache_blobs

    out = {"passes": [], "error": None}
    if not cosmos_writer._is_configured(cfg):
        out["error"] = "auto-heal requires Cosmos DB configuration"
        return out

    for pass_idx in range(max_passes):
        try:
            db = cosmos_writer._get_database_client(cfg)
            container = cosmos_writer._ensure_container(
                db, cosmos_writer.PDF_STATE_CONTAINER,
            )
            incomplete = []
            for item in container.read_all_items():
                status = (item.get("status") or "").lower()
                sf = item.get("source_file") or item.get("id")
                if status in ("partial", "not_started", "failed") and sf:
                    incomplete.append(sf)
        except Exception as exc:
            out["error"] = f"cosmos read failed: {exc}"
            return out

        print()
        print("=" * 70)
        print(f"  HEAL PASS {pass_idx + 1}/{max_passes}: {len(incomplete)} incomplete PDFs")
        print("=" * 70)

        if not incomplete:
            out["passes"].append({"pass": pass_idx + 1, "incomplete": 0, "healed": 0})
            print("  Nothing to heal. Done.")
            return out

        # Purge stale cache for each incomplete PDF so preanalyze re-runs
        # fully (rather than thinking it's already cached).
        all_cache = list_cache_blobs(cfg)
        purged = 0
        for pdf in incomplete:
            prefix = f"_dicache/{pdf}."
            for b in all_cache:
                if b.startswith(prefix):
                    try:
                        if delete_blob(cfg, b):
                            purged += 1
                    except Exception as exc:
                        print(f"  warn: could not delete {b}: {exc}", flush=True)
        print(f"  Purged {purged} cache blobs for {len(incomplete)} PDFs")

        # Re-run preanalyze (will re-process just the purged ones).
        rc, _ = run_step(
            f"heal pass {pass_idx + 1}: preanalyze --incremental",
            [py, str(REPO_ROOT / "scripts" / "preanalyze.py"),
             "--config", config_path, "--incremental"],
            allow_fail=True,
        )

        # Wait for the indexer to pick up the new cache.
        wait_for_indexer_idle(endpoint, indexer_name, max(wait_minutes // 2, 10))

        # Refresh coverage / pdf_state.
        run_step(
            f"heal pass {pass_idx + 1}: check_index --coverage --write-status",
            [py, str(REPO_ROOT / "scripts" / "check_index.py"),
             "--config", config_path, "--coverage", "--write-status",
             "--triggered-by", f"auto-heal-pass-{pass_idx + 1}"],
            allow_fail=True,
        )

        out["passes"].append({
            "pass": pass_idx + 1,
            "incomplete": len(incomplete),
            "purged_blobs": purged,
            "preanalyze_exit": rc,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Run full indexing pipeline")
    ap.add_argument("--config", default="deploy.config.json")
    ap.add_argument("--skip-reconcile", action="store_true")
    ap.add_argument("--skip-preanalyze", action="store_true")
    ap.add_argument("--skip-wait", action="store_true",
                    help="Don't wait for the indexer; just report current state")
    ap.add_argument("--max-wait-minutes", type=int, default=60)
    ap.add_argument("--max-purges", type=int, default=2)
    ap.add_argument("--triggered-by", default="manual",
                    help="Free-text label for the run record (jenkins-cron, jenkins-manual, manual, ...)")
    ap.add_argument("--auto-heal", action="store_true",
                    help="After the main pipeline, audit coverage. If any PDF is "
                         "in PARTIAL or NOT_STARTED state, automatically purge its "
                         "stale cache and re-run preanalyze for it. Repeats up to "
                         "--heal-passes times.")
    ap.add_argument("--heal-passes", type=int, default=2,
                    help="Max number of heal passes (default 2). Each pass: "
                         "identify incomplete PDFs, purge their cache, re-run "
                         "preanalyze on them, wait for indexer.")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    endpoint = cfg["search"]["endpoint"].rstrip("/")
    prefix = cfg["search"].get("artifactPrefix") or "mm-manuals"
    indexer_name = f"{prefix}-indexer"

    pipeline_started_at = _now_iso()
    pipeline_t0 = time.time()
    git_sha = _git_sha()
    py = sys.executable or "python"

    overall_rc = 0
    step_results: dict[str, dict] = {}

    # 1. Reconcile.
    if args.skip_reconcile:
        step_results["reconcile"] = {"skipped": True}
    else:
        rc, _ = run_step(
            "reconcile",
            [py, str(REPO_ROOT / "scripts" / "reconcile.py"),
             "--config", args.config,
             "--max-purges", str(args.max_purges)],
            allow_fail=True,
        )
        step_results["reconcile"] = {"exit_code": rc}
        # Reconcile rc=2 means "would-purge exceeds cap" — that's a soft
        # gate, not a failure. Operator must decide. We continue but
        # mark the pipeline as needing attention.
        if rc not in (0, 2):
            overall_rc = max(overall_rc, rc)

    # 2. Preanalyze (incremental).
    if args.skip_preanalyze:
        step_results["preanalyze"] = {"skipped": True}
    else:
        rc, _ = run_step(
            "preanalyze --incremental",
            [py, str(REPO_ROOT / "scripts" / "preanalyze.py"),
             "--config", args.config,
             "--incremental"],
            allow_fail=True,
        )
        step_results["preanalyze"] = {"exit_code": rc}
        if rc != 0:
            overall_rc = max(overall_rc, rc)

    # 3. Wait for the indexer to drain. The indexer's own 15-minute
    # schedule should pick up new caches automatically; we just watch
    # for it to finish whatever batch it's on.
    if args.skip_wait:
        step_results["wait_indexer"] = {"skipped": True}
    else:
        last = wait_for_indexer_idle(endpoint, indexer_name, args.max_wait_minutes)
        step_results["wait_indexer"] = {
            "last_status": last.get("status"),
            "items_processed": last.get("itemsProcessed", 0),
            "errors": len(last.get("errors") or []),
            "warnings": len(last.get("warnings") or []),
        }

    # 4. Coverage + status persistence. check_index.py --write-status
    # both prints coverage AND writes a run_history + pdf_state batch
    # to Cosmos.
    rc, _ = run_step(
        "check_index --coverage --write-status",
        [py, str(REPO_ROOT / "scripts" / "check_index.py"),
         "--config", args.config,
         "--coverage", "--write-status",
         "--triggered-by", args.triggered_by],
        allow_fail=True,
    )
    step_results["check_index"] = {"exit_code": rc}
    if rc != 0:
        overall_rc = max(overall_rc, rc)

    # 4.5. Optional auto-heal: detect any PDF the indexer didn't fully
    # process (partial chunks, no summary, missing DI cache) and force a
    # clean re-process for just those files. Bounded by --heal-passes
    # so a permanently-broken PDF can't loop forever.
    if args.auto_heal:
        heal_results = _run_heal_passes(
            cfg, args.config, args.heal_passes, args.max_wait_minutes,
            endpoint, indexer_name, py,
        )
        step_results["auto_heal"] = heal_results
        if heal_results.get("error"):
            overall_rc = max(overall_rc, 1)

    # 5. Pipeline-level run record.
    cosmos_writer.write_run_record(cfg, {
        "run_type": "full_pipeline",
        "triggered_by": args.triggered_by,
        "git_sha": git_sha,
        "started_at": pipeline_started_at,
        "duration_seconds": int(time.time() - pipeline_t0),
        "steps": step_results,
        "exit_code": overall_rc,
    })

    print()
    print("=" * 70)
    print(f"  PIPELINE DONE (exit {overall_rc}, "
          f"{int(time.time() - pipeline_t0)}s, "
          f"{sum(1 for s in step_results.values() if s.get('skipped'))} skipped)")
    print("=" * 70)
    return overall_rc


if __name__ == "__main__":
    sys.exit(main())
