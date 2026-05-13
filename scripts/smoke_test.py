"""
Post-deploy validation.

Two modes:

  1. Local mode (--local): no cloud calls. Validates that
       - search/index.json parses and every field has a matching
         indexProjection mapping in skillset.json
       - every output named in skillset.json points at a real index field
       - every projection sourceContext path resolves to a known one
     Useful for catching schema / projection drift in CI before deploy.

  2. Cloud mode (default): triggers the indexer, waits for it, then
     queries the index to validate per-record_type field populations.
     The validation suite covers every field added in Sprints 1-6 so a
     post-deploy run catches any projection wiring that silently dropped
     a field during reindex.

Exits non-zero on any failure so CI can gate on it.

Usage:
    # Cloud mode (runs indexer + validates)
    python scripts/smoke_test.py --config deploy.config.json

    # Cloud mode, skip the run (just validate current state)
    python scripts/smoke_test.py --config deploy.config.json --skip-run

    # Local schema-consistency check (no cloud)
    python scripts/smoke_test.py --local
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

API_VERSION = "2024-05-01-preview"

# ---- Field contracts per record_type ----------------------------------
#
# Each entry lists fields that MUST be populated (non-null, non-empty)
# on a representative sample row of that record_type. The contracts
# capture the additions from Sprints 1-6 so a post-deploy run catches
# any indexProjection wiring that silently dropped a field.
#
# `must_be_populated` = field has a meaningful non-empty value
# `must_exist`        = field is present (may be null/empty if no signal)
# `must_be_list`      = field is a list (collection field)
# `must_be_string`    = field is a string (and present)

FIELD_CONTRACTS: dict[str, dict[str, list[str]]] = {
    "text": {
        "must_be_populated": [
            "chunk_id", "chunk", "physical_pdf_page", "physical_pdf_pages",
            "header_1", "highlight_text", "page_resolution_method",
            "skill_version",
            # Sprint 2
            "embedding_version", "last_indexed_at",
            # Sprint 6 (best-effort: at least one body chunk should
            # have a quality score above 0)
        ],
        "must_exist": [
            # Optional / can be empty when no signal is present.
            "printed_page_label", "printed_page_label_end",
            "figure_ref", "table_ref",
            # Sprint 1
            "callouts", "safety_callout", "footnotes",
            # Sprint 2
            "figures_referenced_normalized", "ocr_min_confidence",
            "document_revision", "effective_date", "document_number",
            # Sprint 4
            "record_subtype", "sections_referenced", "pages_referenced",
            "chunk_token_count",
            # Sprint 6
            "equipment_ids", "language", "chunk_quality_score",
        ],
        "must_be_list": [
            "physical_pdf_pages", "callouts", "footnotes",
            "figures_referenced", "tables_referenced",
            "figures_referenced_normalized",
            "sections_referenced", "pages_referenced",
            "equipment_ids",
        ],
    },
    "diagram": {
        "must_be_populated": [
            "chunk_id", "figure_id", "physical_pdf_page",
            "physical_pdf_pages", "image_hash", "page_resolution_method",
            "skill_version", "figure_bbox",
        ],
        "must_exist": [
            "diagram_description", "diagram_category", "has_diagram",
            "figure_ref",
            # Sprint 2
            "figures_referenced_normalized",
            # Sprint 5
            "image_phash",
        ],
        "must_be_list": [
            "physical_pdf_pages", "figures_referenced_normalized",
        ],
    },
    "table": {
        "must_be_populated": [
            "chunk_id", "chunk", "physical_pdf_page", "physical_pdf_pages",
            "page_resolution_method", "skill_version",
        ],
        "must_exist": [
            "table_caption", "table_row_count", "table_col_count",
            "table_bbox",
            # Sprint 6
            "chunk_content_hash",
        ],
        "must_be_list": ["physical_pdf_pages"],
    },
    "table_row": {
        "must_be_populated": [
            "chunk_id", "chunk", "table_parent_chunk_id",
            "physical_pdf_page", "page_resolution_method", "skill_version",
        ],
        "must_exist": ["table_caption", "table_row_index"],
        "must_be_list": ["physical_pdf_pages"],
    },
    "summary": {
        "must_be_populated": [
            "chunk_id", "page_resolution_method", "skill_version",
        ],
        "must_exist": [
            "chunk", "highlight_text", "pdf_total_pages",
            # Sprint 2
            "document_revision", "effective_date", "document_number",
        ],
        "must_be_list": [],
    },
}


# ---- Local mode: schema consistency without any cloud call ------------

def _load_json(path: Path) -> Any:
    if not path.exists():
        print(f"FAIL: {path} not found")
        sys.exit(2)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"FAIL: {path} is not valid JSON: {exc}")
        sys.exit(2)


def _index_field_names(index: dict) -> set[str]:
    return {f["name"] for f in index.get("fields", [])}


def _projection_target_fields(skillset: dict) -> set[str]:
    """Every `name` referenced in any indexProjection mapping. These must
    exist as real fields in index.json — otherwise the projection drops
    the value silently at indexer runtime."""
    out: set[str] = set()
    proj = (skillset.get("indexProjections") or {}).get("selectors", []) or []
    # Old-style location used by some templates.
    if not proj:
        proj = skillset.get("indexProjections", []) or []
    for sel in proj:
        for m in sel.get("mappings", []) or []:
            n = m.get("name")
            if n:
                out.add(n)
        pkf = sel.get("parentKeyFieldName")
        if pkf:
            out.add(pkf)
    return out


def _skill_output_target_names(skillset: dict) -> set[str]:
    """Every output `targetName` declared by skills in the skillset.
    Used to verify that every field a projection mapping reads from has
    a producing skill output (avoids the common "skill writes X but
    projection reads Y" wiring bug).
    """
    out: set[str] = set()
    for skill in skillset.get("skills", []) or []:
        for o in skill.get("outputs", []) or []:
            tn = o.get("targetName")
            if tn:
                out.add(tn)
    return out


def _run_local(repo_root: Path) -> int:
    print("Running local schema consistency check (no cloud)...")
    index = _load_json(repo_root / "search" / "index.json")
    skillset = _load_json(repo_root / "search" / "skillset.json")

    index_fields = _index_field_names(index)
    proj_fields = _projection_target_fields(skillset)
    skill_outputs = _skill_output_target_names(skillset)

    failures: list[str] = []

    # Every projection mapping name must exist as a real index field.
    missing_in_index = proj_fields - index_fields
    if missing_in_index:
        # Filter common projection-only fields that aren't index fields
        # (parent-key-field names like text_parent_id ARE index fields,
        # but if they're not, that's a problem).
        for f in sorted(missing_in_index):
            failures.append(
                f"projection writes '{f}' but no field in index.json "
                f"by that name — value will silently drop at indexer time"
            )

    # Every projection should have a matching produced source field on
    # the document path (best-effort: we can't fully resolve
    # /document/.../foo paths without a doc, but we can check that
    # named source tokens appear as some skill's targetName).
    proj_source_tokens: set[str] = set()
    proj = (skillset.get("indexProjections") or {}).get("selectors", []) or []
    if not proj:
        proj = skillset.get("indexProjections", []) or []
    for sel in proj:
        for m in sel.get("mappings", []) or []:
            src = m.get("source") or ""
            # Take the last path component as the candidate field name.
            tail = src.rstrip("/").split("/")[-1] if src else ""
            if tail and not tail.startswith("metadata_"):
                proj_source_tokens.add(tail)

    # Each projection source token should be produced by SOME skill or be
    # a known top-level document field (metadata_*, operationalarea, etc).
    KNOWN_DOC_FIELDS = {
        "metadata_storage_path", "metadata_storage_name",
        "operationalarea", "functionalarea", "doctype", "filetype",
        "pdf_total_pages",
    }
    # Known outputs from Azure built-in skills we don't declare ourselves.
    # DocumentIntelligenceLayoutSkill emits the markdownDocument tree
    # with these property names baked in.
    BUILTIN_DI_SKILL_OUTPUTS = {
        "*",                  # wildcard path component
        "h1", "h2", "h3",     # markdownDocument/*/sections/h<n>
        "ordinal_position",   # markdownDocument item ordering
        "pageNumber",         # markdownDocument item page number
        "content",            # markdownDocument item content
        "sections",           # markdownDocument/*/sections
    }
    orphaned_sources = (
        proj_source_tokens - skill_outputs - KNOWN_DOC_FIELDS
        - BUILTIN_DI_SKILL_OUTPUTS - index_fields
    )
    if orphaned_sources:
        for s in sorted(orphaned_sources):
            failures.append(
                f"projection reads '{s}' but no skill output produces it "
                f"(would resolve to null at runtime)"
            )

    # Required field types we depend on per record-type contract — make
    # sure they're declared in index.json.
    for rt, contract in FIELD_CONTRACTS.items():
        for f in contract["must_be_populated"] + contract["must_exist"]:
            if f not in index_fields:
                failures.append(
                    f"contract for record_type='{rt}' references field "
                    f"'{f}' but it's not declared in index.json"
                )

    # Print result.
    if failures:
        print(f"\nLOCAL CHECK FAILED ({len(failures)} issue(s)):")
        for f in failures:
            print(f"  - {f}")
        return 2
    print(
        f"OK. {len(index_fields)} index fields. "
        f"{len(proj_fields)} projection mappings. "
        f"{len(skill_outputs)} skill outputs."
    )
    print("LOCAL CHECK PASSED")
    return 0


# ---- Cloud mode: indexer + per-record validation ----------------------

def _httpx():
    import httpx  # local import so --local doesn't require it
    return httpx


def _aad_token(scope: str) -> str:
    from azure.identity import DefaultAzureCredential
    return DefaultAzureCredential().get_token(scope).token


def _run_indexer(endpoint: str, token: str, indexer_name: str) -> None:
    httpx = _httpx()
    url = f"{endpoint}/indexers/{indexer_name}/run?api-version={API_VERSION}"
    with httpx.Client(timeout=30.0) as c:
        resp = c.post(url, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code not in (200, 202, 204):
        raise SystemExit(f"indexer run failed: {resp.status_code} {resp.text[:500]}")


def _wait_for_indexer(endpoint: str, token: str, indexer_name: str, minutes: int) -> dict:
    httpx = _httpx()
    url = f"{endpoint}/indexers/{indexer_name}/status?api-version={API_VERSION}"
    deadline = time.time() + minutes * 60
    backoff = 5.0
    with httpx.Client(timeout=30.0) as c:
        while time.time() < deadline:
            resp = c.get(url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            last = resp.json().get("lastResult") or {}
            status = last.get("status")
            print(f"  indexer status: {status}")
            if status in ("success", "transientFailure", "persistentFailure"):
                return last
            time.sleep(backoff)
            backoff = min(backoff * 1.3, 30.0)
    raise SystemExit(f"indexer did not complete within {minutes} minutes")


def _record_count(endpoint: str, token: str, index_name: str, filter_expr: str) -> int:
    httpx = _httpx()
    url = f"{endpoint}/indexes/{index_name}/docs/search?api-version={API_VERSION}"
    body = {"search": "*", "filter": filter_expr, "count": True, "top": 0}
    with httpx.Client(timeout=30.0) as c:
        resp = c.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json().get("@odata.count", 0)


def _sample_record(endpoint: str, token: str, index_name: str,
                   filter_expr: str) -> dict | None:
    httpx = _httpx()
    url = f"{endpoint}/indexes/{index_name}/docs/search?api-version={API_VERSION}"
    body = {"search": "*", "filter": filter_expr, "top": 1}
    with httpx.Client(timeout=30.0) as c:
        resp = c.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    hits = resp.json().get("value", [])
    return hits[0] if hits else None


def _facet_distribution(endpoint: str, token: str, index_name: str,
                        field: str, top: int = 20) -> dict[str, int]:
    """Return {value: count} from a facet query. Used to surface things
    like 'how many chunks landed on each page_resolution_method?'."""
    httpx = _httpx()
    url = f"{endpoint}/indexes/{index_name}/docs/search?api-version={API_VERSION}"
    body = {"search": "*", "facets": [f"{field},count:{top}"], "top": 0}
    with httpx.Client(timeout=30.0) as c:
        resp = c.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    facets = (resp.json().get("@search.facets") or {}).get(field, [])
    return {f["value"]: f["count"] for f in facets}


def _check_field_contract(record: dict, contract: dict, record_type: str) -> list[str]:
    """Validate one record against its contract. Returns a list of
    failure messages (empty when all checks pass)."""
    fails: list[str] = []
    for f in contract.get("must_be_populated", []):
        if f not in record:
            fails.append(f"{record_type}.{f}: missing")
            continue
        v = record[f]
        if v is None or v == "":
            fails.append(f"{record_type}.{f}: empty (got {v!r})")
        elif isinstance(v, list) and len(v) == 0:
            fails.append(f"{record_type}.{f}: empty list")

    for f in contract.get("must_exist", []):
        if f not in record:
            fails.append(f"{record_type}.{f}: field absent (projection wiring bug?)")

    for f in contract.get("must_be_list", []):
        if f in record and record[f] is not None and not isinstance(record[f], list):
            fails.append(f"{record_type}.{f}: expected list, got {type(record[f]).__name__}")

    # Page-coverage invariant: physical_pdf_pages must contain start + end.
    pages = record.get("physical_pdf_pages") or []
    start = record.get("physical_pdf_page")
    end = record.get("physical_pdf_page_end")
    if isinstance(pages, list) and pages:
        if start is not None and start not in pages:
            fails.append(
                f"{record_type}.physical_pdf_pages={pages} missing start={start}"
            )
        if end is not None and end not in pages:
            fails.append(
                f"{record_type}.physical_pdf_pages={pages} missing end={end}"
            )

    # figure_bbox / table_bbox / text_bbox must be JSON-serialized lists.
    for bbox_field in ("figure_bbox", "table_bbox", "text_bbox"):
        v = record.get(bbox_field)
        if not v:
            continue
        try:
            parsed = json.loads(v)
        except (TypeError, json.JSONDecodeError):
            fails.append(f"{record_type}.{bbox_field}: not valid JSON")
            continue
        if not isinstance(parsed, list):
            fails.append(
                f"{record_type}.{bbox_field}: expected JSON array (Sprint 2 contract), "
                f"got {type(parsed).__name__}"
            )

    return fails


def _run_cloud(args, repo_root: Path) -> int:
    cfg = _load_json(Path(args.config))
    endpoint = cfg["search"]["endpoint"].rstrip("/")
    prefix = cfg["search"].get("artifactPrefix") or "mm-manuals"
    index_name = f"{prefix}-index"
    indexer_name = f"{prefix}-indexer"

    token = _aad_token("https://search.windows.net/.default")

    if not args.skip_run:
        print(f"Triggering indexer {indexer_name}")
        _run_indexer(endpoint, token, indexer_name)
        print(f"Waiting up to {args.wait_minutes} min for completion")
        last = _wait_for_indexer(endpoint, token, indexer_name, args.wait_minutes)
        if last.get("status") != "success":
            print(json.dumps(last, indent=2)[:2000])
            raise SystemExit(f"indexer finished with status={last.get('status')}")
        items = last.get("itemsProcessed", 0)
        errors = len(last.get("errors") or [])
        warnings = len(last.get("warnings") or [])
        print(f"  items processed: {items}  errors: {errors}  warnings: {warnings}")
        if items == 0:
            raise SystemExit("indexer processed 0 items; no PDFs in the container?")

    failures: list[str] = []

    # Per-record-type counts and field contracts.
    print("\nPer-record-type field contracts")
    for rt, contract in FIELD_CONTRACTS.items():
        count = _record_count(endpoint, token, index_name, f"record_type eq '{rt}'")
        print(f"  {rt}: {count} record(s)")
        if count == 0:
            # table_row is conditional on tables having 5-80 rows. Surface
            # as a warning, not a hard failure.
            if rt == "table_row":
                print(f"    (note: table_row records are only emitted for 5-80-row tables)")
                continue
            failures.append(f"{rt}: zero records in index")
            continue
        sample = _sample_record(endpoint, token, index_name, f"record_type eq '{rt}'")
        if sample is None:
            failures.append(f"{rt}: count>0 but sample fetch returned nothing")
            continue
        rt_fails = _check_field_contract(sample, contract, rt)
        for f in rt_fails:
            failures.append(f)

    # Distribution checks — surface ops-relevant metrics so a bad reindex
    # is visible without log-mining.
    print("\nDistributions (for visibility, not pass/fail)")
    for facet_field in (
        "page_resolution_method", "processing_status",
        "record_subtype", "diagram_category",
    ):
        try:
            dist = _facet_distribution(endpoint, token, index_name, facet_field)
            if dist:
                summary = ", ".join(f"{k}={v}" for k, v in list(dist.items())[:6])
                print(f"  {facet_field}: {summary}")
        except Exception as exc:
            print(f"  {facet_field}: facet query failed ({exc})")

    # Smoke-check the new Sprint-1 safety_callout filter actually returns
    # something — if zero rows have callouts on a real MANGOS manual, the
    # extractor probably regressed.
    callout_count = _record_count(
        endpoint, token, index_name,
        "record_type eq 'text' and safety_callout eq true",
    )
    print(f"\n  text rows with safety_callout=true: {callout_count}")
    if callout_count == 0:
        # Soft warning — depends on corpus content.
        print("    WARN: no chunks flagged with safety_callout. Either the corpus")
        print("          has no WARNING/DANGER/CAUTION text or the extractor regressed.")

    # Sprint 6: synthetic figure auto-extraction
    mupdf_count = _record_count(
        endpoint, token, index_name,
        "record_type eq 'diagram' and figure_id ge 'mupdf_'",
    )
    print(f"  diagram rows from PyMuPDF auto-extract: {mupdf_count}")

    # Result.
    if failures:
        print(f"\nSMOKE TEST FAILED ({len(failures)} issue(s)):")
        for f in failures:
            print(f"  - {f}")
        return 2

    print("\nSMOKE TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="deploy.config.json")
    ap.add_argument("--wait-minutes", type=int, default=15)
    ap.add_argument("--skip-run", action="store_true",
                    help="Skip indexer trigger; validate current index state")
    ap.add_argument("--local", action="store_true",
                    help="Local schema consistency check (no cloud)")
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parent.parent

    if args.local:
        return _run_local(repo_root)
    return _run_cloud(args, repo_root)


if __name__ == "__main__":
    sys.exit(main())
