"""
Local end-to-end simulator.

Drives the actual Azure Function handler functions (process_page_label,
process_diagram, process_semantic_string, process_table, process_doc_summary)
through the exact JSON envelope Azure AI Search sends to a Custom WebApi
skill. Then runs the synthetic records through the same projection
mappings the indexer would apply, and prints one finalized record of each
type (text / diagram / table / summary) plus a multi-page text chunk.

Two things are stubbed because they require live Azure access:
  - shared.aoai.get_client          (Azure OpenAI vision/chat)
  - shared.search_cache.lookup_existing_by_hash (Search REST)

Everything else — semantic string assembly, page-span computation,
chunk_id generation, table shaping, projection mapping — runs as the
real production code path.

Run with:

    python tests/test_e2e_simulator.py
"""

import base64
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "function_app"))

# ---------- stub azure.functions (not installed locally) ----------
import types

if "azure.functions" not in sys.modules:
    fake_az = types.ModuleType("azure")
    fake_func = types.ModuleType("azure.functions")

    class _FakeHttpResponse:
        def __init__(self, body, mimetype="application/json", status_code=200):
            self._body = body if isinstance(body, bytes | bytearray) else body.encode("utf-8")
            self.mimetype = mimetype
            self.status_code = status_code

        def get_body(self):
            return self._body

    class _FakeAuthLevel:
        FUNCTION = "function"

    class _FakeFunctionApp:
        def __init__(self, **kw): pass
        def route(self, **kw):
            def deco(fn): return fn
            return deco

    fake_func.HttpResponse = _FakeHttpResponse
    fake_func.HttpRequest = object
    fake_func.AuthLevel = _FakeAuthLevel
    fake_func.FunctionApp = _FakeFunctionApp
    fake_az.functions = fake_func
    sys.modules["azure"] = fake_az
    sys.modules["azure.functions"] = fake_func


# ---------- stub openai SDK (not installed locally) ----------

if "openai" not in sys.modules:
    fake_openai = types.ModuleType("openai")
    class _FakeAzureOpenAI:
        def __init__(self, **kw): pass
    fake_openai.AzureOpenAI = _FakeAzureOpenAI
    sys.modules["openai"] = fake_openai


# ---------- stub httpx (search_cache imports it) ----------

if "httpx" not in sys.modules:
    fake_httpx = types.ModuleType("httpx")
    class _FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **kw):
            class R:
                status_code = 599
                text = "stub"
                def json(self): return {"value": []}
            return R()
        def get(self, *a, **kw):
            class R:
                status_code = 599
                text = "stub"
                content = b""
            return R()
    class _FakeTimeout:
        def __init__(self, *a, **kw): pass
    class _FakeLimits:
        def __init__(self, *a, **kw): pass
    class _FakeHTTPError(Exception): pass
    class _FakeTimeoutException(_FakeHTTPError): pass
    class _FakeConnectError(_FakeHTTPError): pass
    class _FakeRemoteProtocolError(_FakeHTTPError): pass
    fake_httpx.Client = _FakeClient
    fake_httpx.Timeout = _FakeTimeout
    fake_httpx.Limits = _FakeLimits
    fake_httpx.HTTPError = _FakeHTTPError
    fake_httpx.TimeoutException = _FakeTimeoutException
    fake_httpx.ConnectError = _FakeConnectError
    fake_httpx.RemoteProtocolError = _FakeRemoteProtocolError
    fake_httpx.Response = type("Response", (), {})
    sys.modules["httpx"] = fake_httpx


# ---------- stub Azure OpenAI before importing diagram.py ----------

class _FakeChoice:
    class message:
        content = json.dumps({
            "category": "circuit_diagram",
            "is_useful": True,
            "figure_ref": "Figure 4-2",
            "description": (
                "Schematic showing relay K1 driving contactor C2 through "
                "a 24V control circuit. The diagram labels terminals A1, "
                "A2, and the auxiliary contact 13/14 used for status feedback."
            ),
        })


class _FakeCompletions:
    def create(self, **kw):
        class R:
            choices = [_FakeChoice()]
        return R()


class _FakeChat:
    completions = _FakeCompletions()


class _FakeAOAIClient:
    chat = _FakeChat()


import shared.aoai as aoai_module

aoai_module.get_client = lambda: _FakeAOAIClient()
aoai_module.vision_deployment = lambda: "stub-vision"
aoai_module.chat_deployment = lambda: "stub-chat"

# diagram.py and summary.py do `from .aoai import get_client` so they
# bind their own reference at import time. Patch both AFTER import.
import shared.diagram as diagram_module

diagram_module.get_client = lambda: _FakeAOAIClient()
diagram_module.vision_deployment = lambda: "stub-vision"

import shared.search_cache as cache_module

cache_module.lookup_existing_by_hash = lambda parent_id, image_hash: None

# Now safe to import the rest.
from shared.diagram import process_diagram
from shared.page_label import process_page_label
from shared.process_table import process_table
from shared.semantic import process_semantic_string
from shared.skill_io import handle_skill_request
from shared.summary import process_doc_summary

# ---------- minimal HttpRequest stub ----------

class FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    def get_json(self):
        return self._payload


def call_skill(handler, *records):
    """Drive a handler with the exact envelope shape the indexer sends."""
    payload = {
        "values": [
            {"recordId": str(i), "data": rec}
            for i, rec in enumerate(records)
        ]
    }
    # Bypass the function-app decorator wrapper and call handle_skill_request directly.
    return handle_skill_request(FakeRequest(payload), handler)


def first_record_data(envelope_resp):
    body = json.loads(envelope_resp.get_body().decode("utf-8"))
    return body["values"][0]["data"], body["values"][0].get("errors", [])


# Stub azure.functions.HttpResponse import that handle_skill_request uses.
# It's available at runtime because we already imported it via shared.skill_io.
# Nothing to do here.


# ---------- shared synthetic data ----------

SOURCE_FILE = "manual.pdf"
SOURCE_PATH = "https://example.blob.core.windows.net/manuals/manual.pdf"

# A multi-page section with DI page markers preserved by the built-in
# DocumentIntelligenceLayoutSkill (markdown mode). Two SplitSkill chunks
# will come out of it: one entirely on page 11, one crossing 11->12->13.
SECTION_CONTENT = (
    "<!-- PageNumber=\"11\" -->\n"
    "## 4.2 Control Relay Wiring\n\n"
    "The K1 control relay is wired in line with the start contactor C2. "
    + ("Lorem ipsum dolor sit amet. " * 25)
    + "\n\n"
    + "Figure 4-2 shows the schematic. "
    + ("alpha beta gamma delta. " * 30)
    + "\n<!-- PageBreak -->\n<!-- PageNumber=\"12\" -->\n"
    + "Continue from page 12. "
    + ("epsilon zeta eta theta. " * 40)
    + "\n<!-- PageBreak -->\n<!-- PageNumber=\"13\" -->\n"
    + "Closing remarks on page 13. "
    + ("iota kappa lambda mu. " * 20)
)

# Pretend SplitSkill produced two pages from this section:
CHUNK_PAGE_11 = (
    "## 4.2 Control Relay Wiring\n\n"
    "The K1 control relay is wired in line with the start contactor C2. "
    + ("Lorem ipsum dolor sit amet. " * 25)
)
CHUNK_PAGE_11_TO_13 = (
    "Figure 4-2 shows the schematic. "
    + ("alpha beta gamma delta. " * 30)
    + "\n<!-- PageBreak -->\n<!-- PageNumber=\"12\" -->\n"
    + "Continue from page 12. "
    + ("epsilon zeta eta theta. " * 40)
    + "\n<!-- PageBreak -->\n<!-- PageNumber=\"13\" -->\n"
    + "Closing remarks on page 13. "
    + ("iota kappa lambda mu. " * 20)
)


def divider(label):
    print()
    print("=" * 70)
    print(label)
    print("=" * 70)


# ===========================================================
# 1. TEXT RECORDS — drive extract-page-label + build-semantic-string + projection
# ===========================================================

divider("TEXT RECORDS")

# Page-label skill: chunk 1 (entirely on page 11)
text1_resp = call_skill(
    process_page_label,
    {
        "page_text": CHUNK_PAGE_11,
        "section_content": SECTION_CONTENT,
        "source_file": SOURCE_FILE,
        "source_path": SOURCE_PATH,
        "layout_ordinal": 4,
        "physical_pdf_page": 11,
    },
)
text1_data, _ = first_record_data(text1_resp)

# Page-label skill: chunk 2 (crosses 11 -> 12 -> 13)
text2_resp = call_skill(
    process_page_label,
    {
        "page_text": CHUNK_PAGE_11_TO_13,
        "section_content": SECTION_CONTENT,
        "source_file": SOURCE_FILE,
        "source_path": SOURCE_PATH,
        "layout_ordinal": 4,
        "physical_pdf_page": 11,
    },
)
text2_data, _ = first_record_data(text2_resp)

# Semantic-string skill (text mode) for chunk 2
sem_text_resp = call_skill(
    process_semantic_string,
    {
        "mode": "text",
        "chunk": CHUNK_PAGE_11_TO_13,
        "header_1": "4 Procedures",
        "header_2": "4.2 Control Relay Wiring",
        "header_3": "",
        "source_file": SOURCE_FILE,
        "printed_page_label": text2_data["printed_page_label"],
    },
)
sem_text_data, _ = first_record_data(sem_text_resp)

# Apply the indexer projection mapping for the text selector to get the
# final indexed record. Mirrors search/skillset.json text projection.
final_text_record = {
    "chunk_id": text2_data["chunk_id"],
    "parent_id": text2_data["parent_id"],
    "record_type": text2_data["record_type"],
    "source_file": SOURCE_FILE,
    "source_url": SOURCE_PATH,
    "source_path": SOURCE_PATH,
    "header_1": "4 Procedures",
    "header_2": "4.2 Control Relay Wiring",
    "header_3": "",
    "layout_ordinal": 4,
    "physical_pdf_page": text2_data["physical_pdf_page"],
    "physical_pdf_page_end": text2_data["physical_pdf_page_end"],
    "physical_pdf_pages": text2_data["physical_pdf_pages"],
    "printed_page_label": text2_data["printed_page_label"],
    "printed_page_label_end": text2_data["printed_page_label_end"],
    "chunk": CHUNK_PAGE_11_TO_13[:200] + "...",
    "chunk_for_semantic": sem_text_data["chunk_for_semantic"][:300] + "...",
    "text_vector": "[1536 floats omitted]",
    "processing_status": text2_data["processing_status"],
    "skill_version": text2_data["skill_version"],
}

print("Multi-page text chunk (sample):")
print(json.dumps(final_text_record, indent=2))
print()
print("Single-page text chunk: chunk_id =", text1_data["chunk_id"])
print("Multi-page  text chunk: chunk_id =", text2_data["chunk_id"])
assert text1_data["chunk_id"] != text2_data["chunk_id"], "chunk_id collision!"

print()
print("PAGE-SPAN ASSERTIONS:")
assert text1_data["physical_pdf_page"] == 11, f"chunk1 start should be 11, got {text1_data['physical_pdf_page']}"
assert text1_data["physical_pdf_page_end"] == 11, f"chunk1 end should be 11, got {text1_data['physical_pdf_page_end']}"
print(f"  chunk1 (single-page): physical_pdf_page={text1_data['physical_pdf_page']}, end={text1_data['physical_pdf_page_end']}  OK")
assert text2_data["physical_pdf_page"] == 11, f"chunk2 start should be 11, got {text2_data['physical_pdf_page']}"
assert text2_data["physical_pdf_page_end"] == 13, f"chunk2 end should be 13, got {text2_data['physical_pdf_page_end']}"
assert text2_data["physical_pdf_pages"] == [11, 12, 13], (
    f"chunk2 pages should be [11,12,13], got {text2_data['physical_pdf_pages']}"
)
print(f"  chunk2 (multi-page):  physical_pdf_page={text2_data['physical_pdf_page']}, end={text2_data['physical_pdf_page_end']}, pages={text2_data['physical_pdf_pages']}  OK")


# ===========================================================
# 2. DIAGRAM RECORD — drive analyze-diagram + build-semantic-string + projection
# ===========================================================

divider("DIAGRAM RECORD")

# Tiny PNG (1x1 transparent) so the hash path is exercised end-to-end.
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9d4xR8YA"
    "AAAASUVORK5CYII="
)

dgm_resp = call_skill(
    process_diagram,
    {
        "image_b64": TINY_PNG_B64,
        "figure_id": "fig_0",
        "page_number": 12,
        "caption": "Figure 4-2: Control relay wiring",
        "bbox": {"page": 12, "x_in": 1.2, "y_in": 3.4, "w_in": 4.0, "h_in": 2.5},
        "header_1": "4 Procedures",
        "header_2": "4.2 Control Relay Wiring",
        "header_3": "",
        "surrounding_context": (
            "Figure 4-2 shows the schematic of the K1 control relay wiring. "
            "The relay is energized through a 24V auxiliary supply..."
        ),
        "source_file": SOURCE_FILE,
        "source_path": SOURCE_PATH,
        "parent_id": "abc123def456",
    },
)
dgm_data, dgm_errors = first_record_data(dgm_resp)
assert not dgm_errors, f"diagram errors: {dgm_errors}"

dgm_sem_resp = call_skill(
    process_semantic_string,
    {
        "mode": "diagram",
        "diagram_description": dgm_data["diagram_description"],
        "diagram_category": dgm_data["diagram_category"],
        "figure_ref": dgm_data["figure_ref"],
        "context_text": dgm_data["surrounding_context"],
        "source_file": SOURCE_FILE,
        "physical_pdf_page": str(dgm_data["physical_pdf_page"]),
    },
)
dgm_sem_data, _ = first_record_data(dgm_sem_resp)

final_diagram_record = {
    "chunk_id": dgm_data["chunk_id"],
    "parent_id": dgm_data["parent_id"],
    "record_type": dgm_data["record_type"],
    "source_file": SOURCE_FILE,
    "source_url": SOURCE_PATH,
    "source_path": SOURCE_PATH,
    "physical_pdf_page": dgm_data["physical_pdf_page"],
    "physical_pdf_page_end": dgm_data["physical_pdf_page_end"],
    "header_1": dgm_data["header_1"],
    "header_2": dgm_data["header_2"],
    "header_3": dgm_data["header_3"],
    "figure_id": dgm_data["figure_id"],
    "figure_bbox": dgm_data["figure_bbox"],
    "figure_ref": dgm_data["figure_ref"],
    "diagram_category": dgm_data["diagram_category"],
    "has_diagram": dgm_data["has_diagram"],
    "diagram_description": dgm_data["diagram_description"],
    "surrounding_context": dgm_data["surrounding_context"][:200] + "...",
    "image_hash": dgm_data["image_hash"],
    "chunk": dgm_data["diagram_description"][:200] + "...",
    "chunk_for_semantic": dgm_sem_data["chunk_for_semantic"][:300] + "...",
    "text_vector": "[1536 floats omitted]",
    "processing_status": dgm_data["processing_status"],
    "skill_version": dgm_data["skill_version"],
}
print(json.dumps(final_diagram_record, indent=2))

print()
print("DIAGRAM ASSERTIONS:")
assert dgm_data["chunk_id"].startswith("dgm_"), "diagram id prefix wrong"
assert dgm_data["record_type"] == "diagram", "record_type wrong"
assert dgm_data["has_diagram"] is True, "has_diagram should be True for circuit_diagram"
assert dgm_data["diagram_category"] == "circuit_diagram", f"category got {dgm_data['diagram_category']}"
assert dgm_data["figure_ref"] == "Figure 4-2", f"figure_ref got {dgm_data['figure_ref']}"
assert dgm_data["header_1"] == "4 Procedures"
assert dgm_data["header_2"] == "4.2 Control Relay Wiring"
assert "K1" in dgm_data["surrounding_context"]
assert "K1" in dgm_sem_data["chunk_for_semantic"]
print("  prefix OK | category OK | figure_ref OK | headers OK | context propagated OK")


# ===========================================================
# 3. TABLE RECORD — drive shape-table + projection
# ===========================================================

divider("TABLE RECORD")

table_md = (
    "| Parameter | Value | Units |\n"
    "| --- | --- | --- |\n"
    "| Voltage | 24 | V |\n"
    "| Current | 1.5 | A |"
)

tbl_resp = call_skill(
    process_table,
    {
        "table_index": "0_0",
        "page_start": 14,
        "page_end": 14,
        "markdown": table_md,
        "row_count": 3,
        "col_count": 3,
        "caption": "Table 5: Control relay specifications",
        "header_1": "4 Procedures",
        "header_2": "4.3 Specifications",
        "header_3": "",
        "source_file": SOURCE_FILE,
        "source_path": SOURCE_PATH,
        "parent_id": "abc123def456",
    },
)
tbl_data, _ = first_record_data(tbl_resp)

final_table_record = {
    "chunk_id": tbl_data["chunk_id"],
    "parent_id": tbl_data["parent_id"],
    "record_type": tbl_data["record_type"],
    "source_file": SOURCE_FILE,
    "source_url": SOURCE_PATH,
    "source_path": SOURCE_PATH,
    "header_1": tbl_data["header_1"],
    "header_2": tbl_data["header_2"],
    "header_3": tbl_data["header_3"],
    "physical_pdf_page": tbl_data["physical_pdf_page"],
    "physical_pdf_page_end": tbl_data["physical_pdf_page_end"],
    "physical_pdf_pages": tbl_data["physical_pdf_pages"],
    "table_row_count": tbl_data["table_row_count"],
    "table_col_count": tbl_data["table_col_count"],
    "table_caption": tbl_data["table_caption"],
    "chunk": tbl_data["chunk"],
    "chunk_for_semantic": tbl_data["chunk_for_semantic"],
    "text_vector": "[1536 floats omitted]",
    "processing_status": tbl_data["processing_status"],
    "skill_version": tbl_data["skill_version"],
}
print(json.dumps(final_table_record, indent=2))

print()
print("TABLE ASSERTIONS:")
assert tbl_data["chunk_id"].startswith("tbl_")
assert tbl_data["record_type"] == "table"
assert tbl_data["table_caption"] == "Table 5: Control relay specifications"
assert "figure_ref" not in tbl_data, "figure_ref should NOT be on table records"
assert "| Voltage |" in tbl_data["chunk"], "markdown grid missing in chunk"
assert "Section: 4 Procedures > 4.3 Specifications" in tbl_data["chunk_for_semantic"]
assert "Table 5: Control relay specifications" in tbl_data["chunk_for_semantic"]
print("  prefix OK | caption first-class OK | no figure_ref overload OK | markdown preserved OK")


# ===========================================================
# 4. SUMMARY RECORD — drive build-doc-summary + projection
# ===========================================================

divider("SUMMARY RECORD")

# Stub the summary chat call to avoid live AOAI.
# build-doc-summary uses aoai.get_client() which we already stubbed above
# to return a circuit_diagram JSON. Reuse the same fake but make
# completions.create return a plain prose summary string.

class _SummaryFakeChoice:
    class message:
        content = (
            "This manual covers the installation and operation of the K-series "
            "control relay system. It includes wiring procedures (Section 4.2), "
            "specification tables (Section 4.3), and safety notes throughout. "
            "Notable figures include Figure 4-2 showing the control relay schematic."
        )


class _SummaryFakeCompletions:
    def create(self, **kw):
        class R:
            choices = [_SummaryFakeChoice()]
        return R()


class _SummaryFakeChat:
    completions = _SummaryFakeCompletions()


class _SummaryFakeAOAIClient:
    chat = _SummaryFakeChat()


# Hot-swap the client BOTH at module level and at summary's own binding
# (summary.py did `from .aoai import get_client` so it has its own ref).
import shared.summary as summary_module

aoai_module.get_client = lambda: _SummaryFakeAOAIClient()
summary_module.get_client = lambda: _SummaryFakeAOAIClient()

sum_resp = call_skill(
    process_doc_summary,
    {
        "source_file": SOURCE_FILE,
        "source_path": SOURCE_PATH,
        "markdown_text": [
            "# Manual\n\n## 1 Overview\n\nIntroduction to the K-series.",
            "## 4.2 Control Relay Wiring\n\nWiring procedure...",
            "## 4.3 Specifications\n\nSee Table 5.",
        ],
        "section_titles": ["1 Overview", "4 Procedures"],
    },
)
sum_data, _ = first_record_data(sum_resp)

final_summary_record = {
    "chunk_id": sum_data["chunk_id"],
    "parent_id": sum_data["parent_id"],
    "record_type": sum_data["record_type"],
    "source_file": SOURCE_FILE,
    "source_url": SOURCE_PATH,
    "source_path": SOURCE_PATH,
    "chunk": sum_data["chunk"],
    "chunk_for_semantic": sum_data["chunk_for_semantic"],
    "text_vector": "[1536 floats omitted]",
    "processing_status": sum_data["processing_status"],
    "skill_version": sum_data["skill_version"],
}
print(json.dumps(final_summary_record, indent=2))

print()
print("SUMMARY ASSERTIONS:")
assert sum_data["chunk_id"].startswith("sum_")
assert sum_data["record_type"] == "summary"
assert sum_data["processing_status"] == "ok"
assert "K-series" in sum_data["chunk"]
print("  prefix OK | status=ok OK | content propagated OK")


# ===========================================================
# CROSS-RECORD CONSISTENCY
# ===========================================================

divider("CROSS-RECORD CONSISTENCY")

all_ids = [
    text1_data["chunk_id"],
    text2_data["chunk_id"],
    dgm_data["chunk_id"],
    tbl_data["chunk_id"],
    sum_data["chunk_id"],
]
print("All five sample chunk_ids:")
for cid in all_ids:
    print("  ", cid)
assert len(all_ids) == len(set(all_ids)), "chunk_id collision across record types!"
prefixes = {cid.split("_", 1)[0] for cid in all_ids}
print("Distinct prefixes:", prefixes)
assert prefixes == {"txt", "dgm", "tbl", "sum"}, f"unexpected prefixes: {prefixes}"

# Validate every projected record carries fields that exist in index.json
idx = json.load(open(os.path.join(os.path.dirname(__file__), "..", "search", "index.json")))
idx_field_names = {f["name"] for f in idx["fields"]}

def assert_subset(label, record):
    extras = [k for k in record if k not in idx_field_names]
    assert not extras, f"{label} has fields not in index: {extras}"
    print(f"  {label}: all {len(record)} projected fields exist in index")

assert_subset("text record",    final_text_record)
assert_subset("diagram record", final_diagram_record)
assert_subset("table record",   final_table_record)
assert_subset("summary record", final_summary_record)

print()
print("=" * 70)
print("ALL E2E SIMULATION CHECKS PASSED")
print("=" * 70)
