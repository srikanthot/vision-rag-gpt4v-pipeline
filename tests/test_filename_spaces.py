"""
Regression test for the filename-with-spaces bug.

History: PDFs whose names contained spaces (e.g. "Gas Restoration Flood
Playbook.pdf") triggered HTTP 403 from Azure Storage in two places:

  1. scripts/preanalyze.py — SharedKey signing built the URL with literal
     spaces. httpx encoded them on the wire as %20, but the canonical
     resource string used for signing left them as spaces (correct), so
     the signature itself was fine. The actual failure mode: certain
     httpx/httpcore versions did not encode the URL path consistently,
     producing a malformed request line.

  2. function_app/shared/di_client.py — same class of bug in four cache
     fetch helpers; the cache URL was string-concatenated without any
     URL encoding, so a space in the source filename produced an invalid
     URL passed to httpx.

Both fixes go through urllib.parse.quote() to ensure the URL path is
always properly percent-encoded.

These tests are pure-Python — they don't hit Azure. They verify the
URL-construction logic only.
"""

import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "function_app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import preanalyze  # noqa: E402  -- after sys.path manipulation
from shared.di_client import (  # noqa: E402
    _build_cache_url,
    _build_cache_url_with_id,
    _split_blob_url,
)

# ---------- function_app/shared/di_client.py ----------

def test_split_blob_url_with_simple_name():
    base, fname = _split_blob_url(
        "https://acc.blob.core.windows.net/container/manual.pdf"
    )
    assert base == "https://acc.blob.core.windows.net/container", base
    assert fname == "manual.pdf", fname


def test_split_blob_url_preserves_url_encoding_from_input():
    """If Azure Search supplies a URL with %20, we keep it that way for
    cache URL construction. We do NOT decode-and-reencode (which would
    risk double-encoding for filenames that legitimately contain '%')."""
    base, fname = _split_blob_url(
        "https://acc.blob.core.windows.net/container/Gas%20Restoration%20Flood%20Playbook.pdf"
    )
    assert fname == "Gas%20Restoration%20Flood%20Playbook.pdf", fname


def test_split_blob_url_returns_none_on_garbage():
    assert _split_blob_url("") is None
    # No path = nothing to extract.
    assert _split_blob_url("https://acc.blob.core.windows.net") is None


def test_build_cache_url_appends_dicache_suffix():
    url = _build_cache_url(
        "https://acc.blob.core.windows.net/container/manual.pdf",
        "di.json",
    )
    assert url == (
        "https://acc.blob.core.windows.net/container/_dicache/manual.pdf.di.json"
    ), url


def test_build_cache_url_preserves_encoded_filename():
    """Names with spaces — the original bug — must produce a properly
    encoded cache URL."""
    url = _build_cache_url(
        "https://acc.blob.core.windows.net/container/Gas%20Restoration%20Flood%20Playbook.pdf",
        "di.json",
    )
    assert (
        url
        == "https://acc.blob.core.windows.net/container/_dicache/"
           "Gas%20Restoration%20Flood%20Playbook.pdf.di.json"
    ), url
    # Critical: no literal space anywhere in the URL.
    assert " " not in url


def test_build_cache_url_with_id_encodes_id_value():
    """Figure IDs come from DI; defensive encoding ensures they don't
    break the URL even if DI returns weird IDs."""
    url = _build_cache_url_with_id(
        "https://acc.blob.core.windows.net/container/manual.pdf",
        "vision",
        "fig 7/2",  # contains space and slash
    )
    # Slash inside the figure id MUST be encoded — otherwise it becomes a
    # path separator and the cache lookup hits a different blob.
    assert "fig%207%2F2" in url, url
    assert " " not in url


# ---------- scripts/preanalyze.py ----------

def test_preanalyze_blob_url_encodes_spaces():
    """Calling _blob_url with a name containing spaces must produce a
    URL with %20, never a literal space."""
    preanalyze._storage_account_name = "acc"
    preanalyze._storage_endpoint_suffix = "core.windows.net"
    url = preanalyze._blob_url("container", "Gas Restoration Flood Playbook.pdf")
    assert url == (
        "https://acc.blob.core.windows.net/container/"
        "Gas%20Restoration%20Flood%20Playbook.pdf"
    ), url
    assert " " not in url


def test_preanalyze_blob_url_encodes_special_chars():
    """Other URL-unsafe characters must also be encoded."""
    preanalyze._storage_account_name = "acc"
    preanalyze._storage_endpoint_suffix = "core.windows.net"
    url = preanalyze._blob_url("container", "manual (v2)#draft.pdf")
    # "(" and ")" are URL-safe per RFC 3986; "#" is a fragment delimiter
    # and MUST be encoded inside paths.
    assert "%23" in url, url  # # encoded
    assert " " not in url


def test_preanalyze_blob_url_preserves_dicache_path_separator():
    """Cache blob names contain a slash (e.g. _dicache/foo.pdf.di.json).
    quote(safe="/") must preserve that — we don't want it encoded as %2F."""
    preanalyze._storage_account_name = "acc"
    preanalyze._storage_endpoint_suffix = "core.windows.net"
    url = preanalyze._blob_url("container", "_dicache/manual.pdf.di.json")
    assert url == (
        "https://acc.blob.core.windows.net/container/"
        "_dicache/manual.pdf.di.json"
    ), url


# ---------- runner ----------

def main():
    tests = [
        test_split_blob_url_with_simple_name,
        test_split_blob_url_preserves_url_encoding_from_input,
        test_split_blob_url_returns_none_on_garbage,
        test_build_cache_url_appends_dicache_suffix,
        test_build_cache_url_preserves_encoded_filename,
        test_build_cache_url_with_id_encodes_id_value,
        test_preanalyze_blob_url_encodes_spaces,
        test_preanalyze_blob_url_encodes_special_chars,
        test_preanalyze_blob_url_preserves_dicache_path_separator,
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
