"""
Verify tech-manual content captures the things readers actually search for:

  - Figure / Table references already covered by test_unit.py
  - Equation references (NEW)
  - Section number references (NEW)
  - Safety callouts head-loaded for retrieval (NEW)
  - Running artifacts (revision lines, copyright, document IDs) stripped
    before embedding (NEW)

These are pure-Python tests; no Azure dependencies.
"""

import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "function_app"))

from shared.sections import _strip_running_artifacts  # noqa: E402
from shared.semantic import (  # noqa: E402
    _extract_callouts,
    _extract_equation_refs,
    _extract_section_refs,
    process_semantic_string,
)


def test_equation_refs_extracted():
    text = "Per Equation 4-2 the load is given by V = I*R. See also Eq. 18.3 and Equation A-1."
    refs = _extract_equation_refs(text)
    assert "Equation 4-2" in refs, refs
    assert "Equation 18.3" in refs, refs
    assert "Equation A-1" in refs, refs


def test_section_refs_extracted():
    text = (
        "See Section 4.2 for full procedure. Note that Sec. 18-3 is superseded "
        "by § 4.2.1, which references Section 5."
    )
    refs = _extract_section_refs(text)
    # Hyphens and dots are both legitimate in technical-manual section IDs.
    assert "Section 4.2" in refs, refs
    assert "Section 4.2.1" in refs, refs
    assert "Section 18-3" in refs, refs
    assert "Section 5" in refs, refs


def test_callouts_extracted():
    text = (
        "Open the panel. WARNING: high voltage present, de-energize first.\n"
        "Insert the cable. CAUTION - heavy load, two-person lift required.\n"
        "Connect the leads. NOTE: torque to spec."
    )
    callouts = _extract_callouts(text)
    assert any("WARNING" in c for c in callouts), callouts
    assert any("CAUTION" in c for c in callouts), callouts
    assert any("NOTE" in c for c in callouts), callouts


def test_callouts_capped_at_three():
    text = "\n".join([f"WARNING: thing {i}" for i in range(10)])
    callouts = _extract_callouts(text)
    assert len(callouts) == 3, callouts


def test_running_artifacts_strips_revision_line():
    text = "Revision 3.2\nThis is body text.\nRev. 4 -- March 2024\nMore body."
    cleaned = _strip_running_artifacts(text)
    assert "This is body text." in cleaned
    assert "More body." in cleaned
    assert "Revision 3.2" not in cleaned, cleaned
    assert "Rev. 4" not in cleaned, cleaned


def test_running_artifacts_strips_copyright():
    text = "Copyright 2024 MANGOS\nBody content here.\n© 2023 ACME Corp"
    cleaned = _strip_running_artifacts(text)
    assert "Body content here." in cleaned
    assert "Copyright" not in cleaned, cleaned
    assert "©" not in cleaned, cleaned


def test_running_artifacts_strips_doc_ids():
    text = "GD-AS-ATM-001\nReal section content.\nDOC-12345"
    cleaned = _strip_running_artifacts(text)
    assert "Real section content." in cleaned
    assert "GD-AS-ATM-001" not in cleaned, cleaned
    assert "DOC-12345" not in cleaned, cleaned


def test_running_artifacts_strips_dates():
    text = "March 2024\nReal procedure step.\n2024-03-15\n03/15/2024"
    cleaned = _strip_running_artifacts(text)
    assert "Real procedure step." in cleaned
    assert "March 2024" not in cleaned, cleaned
    assert "2024-03-15" not in cleaned, cleaned
    assert "03/15/2024" not in cleaned, cleaned


def test_running_artifacts_strips_confidential_stamps():
    text = "Confidential\nReal content.\nProprietary\nFor Internal Use Only"
    cleaned = _strip_running_artifacts(text)
    assert "Real content." in cleaned
    assert "Confidential" not in cleaned, cleaned
    assert "Proprietary" not in cleaned, cleaned


def test_running_artifacts_preserves_inline_artifact_text():
    """A line that has artifact-looking text inline with real content
    must NOT be stripped — the body is too valuable."""
    text = "After step 5, see Page 215 for details on the thermal cutoff."
    cleaned = _strip_running_artifacts(text)
    assert "thermal cutoff" in cleaned, cleaned


def test_semantic_string_includes_callouts_and_equation_refs():
    """End-to-end: build the embedded form for a chunk that contains
    a warning callout, an equation reference, and a section reference.
    All three should appear in the head-loaded portion of the output."""
    out = process_semantic_string({
        "mode": "text",
        "source_file": "manual.pdf",
        "header_1": "Chapter 4",
        "header_2": "Voltage Regulation",
        "header_3": "",
        "printed_page_label": "4-12",
        "figure_ref": "",
        "table_ref": "",
        "chunk": (
            "Per Equation 4-2 the load voltage is constant. See Section 4.2 "
            "for the derivation. WARNING: do not bypass the regulator without "
            "first opening the upstream breaker."
        ),
    })
    semantic = out["chunk_for_semantic"]
    assert "References:" in semantic, semantic
    assert "Equation 4-2" in semantic, semantic
    assert "Section 4.2" in semantic, semantic
    assert "Callouts:" in semantic, semantic
    assert "WARNING" in semantic, semantic


def test_semantic_string_no_dead_lines_when_no_refs():
    """When a chunk has no refs and no callouts, the head-loaded lines
    must not appear at all (empty References:/Callouts: lines would
    waste embedding budget on noise)."""
    out = process_semantic_string({
        "mode": "text",
        "source_file": "manual.pdf",
        "header_1": "Intro",
        "header_2": "",
        "header_3": "",
        "printed_page_label": "1",
        "figure_ref": "",
        "table_ref": "",
        "chunk": "Plain prose with nothing notable in it.",
    })
    semantic = out["chunk_for_semantic"]
    assert "References:" not in semantic, semantic
    assert "Callouts:" not in semantic, semantic


def test_pdf_crop_raises_corrupt_for_garbage_bytes():
    """A clearly-bad PDF triggers CorruptPdfError, not a silent empty
    crop. The operator needs to know; bad input gets surfaced."""
    from shared.pdf_crop import CorruptPdfError, _open_pdf
    try:
        _open_pdf(b"this is not a pdf")
        raise AssertionError("should have raised CorruptPdfError")
    except CorruptPdfError:
        pass


def test_vision_validation_short_description_retried():
    """A vision response with category=schematic and is_useful=true but
    a 5-char description is degenerate — the validation gate must
    invoke retry, then accept whatever the retry returns."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import preanalyze

    # The validator's retry is the FIRST call into the stub (the
    # original short response was passed in as vision_result already).
    # Stub returns a longer description so the retry succeeds.
    call_count = {"n": 0}

    def fake_call(cfg, image_b64, user_text, max_retries=3):
        call_count["n"] += 1
        return {"category": "schematic", "is_useful": True,
                "description": "Wiring schematic showing breaker B1 to TB-1.",
                "figure_ref": "", "ocr_text": ""}

    original_call = preanalyze._call_vision_api
    preanalyze._call_vision_api = fake_call
    try:
        result = preanalyze._validate_and_retry_if_degenerate(
            cfg={}, image_b64="", user_text="describe", fig_id="fig_test",
            vision_result={"category": "schematic", "is_useful": True,
                           "description": "OK", "figure_ref": "", "ocr_text": ""},
        )
        assert "breaker" in result["description"], result
        # The validator called the API exactly once on retry (the original
        # call was outside this function).
        assert call_count["n"] == 1, call_count
    finally:
        preanalyze._call_vision_api = original_call


def test_vision_validation_decorative_short_is_OK():
    """Short description on a decorative figure is FINE -- no retry.
    This is the 'logo' case: vision says decorative, no retry needed."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import preanalyze

    call_count = {"n": 0}

    def should_not_call(cfg, image_b64, user_text, max_retries=3):
        call_count["n"] += 1
        return {}

    original_call = preanalyze._call_vision_api
    preanalyze._call_vision_api = should_not_call
    try:
        result = preanalyze._validate_and_retry_if_degenerate(
            cfg={}, image_b64="", user_text="describe", fig_id="fig_test",
            vision_result={"category": "decorative", "is_useful": False,
                           "description": "logo", "figure_ref": "", "ocr_text": ""},
        )
        # No retry for decorative — accepted as-is.
        assert call_count["n"] == 0, call_count
        assert result["description"] == "logo"
    finally:
        preanalyze._call_vision_api = original_call


def test_convert_module_pure_imports():
    """convert.py must import without LibreOffice present, and expose
    is_available() / needs_conversion() / convert_to_pdf() as the
    public API."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import convert
    assert hasattr(convert, "is_available")
    assert hasattr(convert, "needs_conversion")
    assert hasattr(convert, "convert_to_pdf")
    assert hasattr(convert, "ConverterNotAvailable")
    assert hasattr(convert, "ConversionError")


def test_convert_needs_conversion():
    """Only non-PDFs need conversion; PDFs pass through."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import convert
    assert convert.needs_conversion("manual.docx") is True
    assert convert.needs_conversion("manual.pptx") is True
    assert convert.needs_conversion("sheet.xlsx") is True
    assert convert.needs_conversion("manual.pdf") is False
    assert convert.needs_conversion("MANUAL.PDF") is False  # case-insensitive


def test_convert_pdf_passthrough_returns_unchanged():
    """convert_to_pdf on a PDF must return the input unchanged without
    invoking LibreOffice (so PDFs work even without conversion installed)."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import convert
    fake_pdf_bytes = b"%PDF-1.7 ... fake bytes"
    out = convert.convert_to_pdf("file.pdf", fake_pdf_bytes)
    assert out == fake_pdf_bytes


def test_convert_raises_converter_not_available_when_libreoffice_missing():
    """When LibreOffice is genuinely missing, calling convert_to_pdf
    on a non-PDF must raise ConverterNotAvailable (not ConversionError),
    so the caller can distinguish 'install LibreOffice' from 'this file
    is corrupt'."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import convert

    # Stub the binary lookup to simulate "not installed" regardless of
    # whether the test agent has LibreOffice.
    original = convert._libreoffice_binary
    def raise_not_avail():
        raise convert.ConverterNotAvailable("simulated missing for test")
    convert._libreoffice_binary = raise_not_avail
    try:
        try:
            convert.convert_to_pdf("manual.pptx", b"fake pptx bytes")
            raise AssertionError("should have raised ConverterNotAvailable")
        except convert.ConverterNotAvailable:
            pass
    finally:
        convert._libreoffice_binary = original


def test_bootstrap_module_importable():
    """bootstrap.py must import cleanly and expose the expected helpers."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import bootstrap
    assert hasattr(bootstrap, "main"), "bootstrap.py must have main()"
    assert hasattr(bootstrap, "detect_search_service_config"), "missing detect_search_service_config"
    assert hasattr(bootstrap, "detect_cosmos_database"), "missing detect_cosmos_database"


def test_pipeline_lock_module_importable():
    """The pipeline_lock module must import cleanly (no syntax errors,
    no missing imports). We don't run live lock acquire/release here
    because that hits Azure storage."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import importlib
    mod = importlib.import_module("pipeline_lock")
    # Public surface contract.
    assert hasattr(mod, "acquire_lock"), "missing acquire_lock"
    assert hasattr(mod, "release_lock"), "missing release_lock"
    assert hasattr(mod, "PipelineLock"), "missing PipelineLock context manager"
    assert hasattr(mod, "LockHeldError"), "missing LockHeldError exception"


def test_pipeline_lock_blob_name_namespacing():
    """Lock blobs live under _dicache/ and start with .lock- so they
    are easy to grep AND covered by reconcile's cache-blob accounting."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import pipeline_lock
    name = pipeline_lock._lock_blob_name("preanalyze")
    assert name == "_dicache/.lock-preanalyze.json", name
    assert name.startswith("_dicache/.lock-")


def test_supported_extensions_in_preanalyze_includes_office_formats():
    """PPTX/DOCX/XLSX must be recognised as supported by preanalyze's
    file-listing filter."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import preanalyze

    assert ".pdf" in preanalyze.SUPPORTED_EXTENSIONS
    assert ".docx" in preanalyze.SUPPORTED_EXTENSIONS
    assert ".pptx" in preanalyze.SUPPORTED_EXTENSIONS
    assert ".xlsx" in preanalyze.SUPPORTED_EXTENSIONS

    assert preanalyze._is_pdf("manual.pdf") is True
    assert preanalyze._is_pdf("manual.PDF") is True
    assert preanalyze._is_pdf("slides.pptx") is False
    assert preanalyze._is_pdf("doc.docx") is False
    assert preanalyze._is_pdf("sheet.xlsx") is False


def main():
    tests = [
        test_equation_refs_extracted,
        test_section_refs_extracted,
        test_callouts_extracted,
        test_callouts_capped_at_three,
        test_running_artifacts_strips_revision_line,
        test_running_artifacts_strips_copyright,
        test_running_artifacts_strips_doc_ids,
        test_running_artifacts_strips_dates,
        test_running_artifacts_strips_confidential_stamps,
        test_running_artifacts_preserves_inline_artifact_text,
        test_semantic_string_includes_callouts_and_equation_refs,
        test_semantic_string_no_dead_lines_when_no_refs,
        test_pdf_crop_raises_corrupt_for_garbage_bytes,
        test_vision_validation_short_description_retried,
        test_vision_validation_decorative_short_is_OK,
        test_convert_module_pure_imports,
        test_convert_needs_conversion,
        test_convert_pdf_passthrough_returns_unchanged,
        test_convert_raises_converter_not_available_when_libreoffice_missing,
        test_bootstrap_module_importable,
        test_pipeline_lock_module_importable,
        test_pipeline_lock_blob_name_namespacing,
        test_supported_extensions_in_preanalyze_includes_office_formats,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception:
            print(f"  FAIL  {t.__name__}")
            traceback.print_exc()
            failed += 1
    print()
    if failed:
        print(f"{failed}/{len(tests)} test(s) FAILED")
        sys.exit(1)
    print(f"{len(tests)} test(s) passed")


if __name__ == "__main__":
    main()
