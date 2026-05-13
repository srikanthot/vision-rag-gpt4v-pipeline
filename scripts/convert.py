"""
Convert DOCX / PPTX / XLSX to PDF so the rest of the pipeline (DI cache,
PyMuPDF cropping, GPT-4 Vision on figures) treats them uniformly.

Why this exists
---------------
Document Intelligence's prebuilt-layout natively handles all four
formats (PDF/DOCX/PPTX/XLSX) and returns figures + tables. But our
figure-cropping step uses PyMuPDF, which only renders PDFs. Without a
conversion step, slides and document images go un-analyzed — the user
loses figure descriptions for every PPTX/DOCX/XLSX manual.

Solution: run LibreOffice headless on the agent to produce a PDF, then
pipe it through the existing PDF flow. The converted PDF is a transient
computation artifact; the cache (DI / crop / output) is keyed by the
ORIGINAL filename so process_document.py at runtime sees the same name
the indexer projected.

Why LibreOffice
---------------
- Free, open source, no licensing concerns
- Cross-platform (Windows / Linux / Mac)
- Converts DOCX, PPTX, XLSX with reasonable fidelity
- Single binary; no Python deps to manage
- Already installed on most Linux build agents

Limitations / known caveats
---------------------------
- Conversion fidelity is "good enough", not pixel-perfect:
    * PowerPoint animations are flattened (irrelevant for retrieval)
    * Excel charts may render with slight offset
    * Embedded fonts may be substituted
  These don't affect text/table extraction; they may shift figure
  bounding boxes by a few pixels (usually fine for crop-then-vision).
- Conversion takes 5-30 seconds per file. Negligible vs DI which is
  minutes per file.
- LibreOffice can hang on malformed inputs; we apply a 5-minute timeout
  and treat hang as a hard fail.

Disabling conversion
--------------------
If LibreOffice is not installed on the agent, conversion is skipped and
preanalyze degrades to the previous behavior (text + tables only for
non-PDF formats). preflight.py will warn the operator.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class ConversionError(Exception):
    """LibreOffice produced no output, hung, or refused the input."""


class ConverterNotAvailable(Exception):
    """LibreOffice is not on PATH. Caller can choose to fall back to
    PDF-only processing."""


def _libreoffice_binary() -> str:
    """Return the LibreOffice headless binary name. On Windows the
    install path may not be on PATH; fall back to the standard install
    location if that's the case."""
    for candidate in ("libreoffice", "soffice"):
        if shutil.which(candidate):
            return candidate
    if os.name == "nt":
        for path in (
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ):
            if Path(path).exists():
                return path
    raise ConverterNotAvailable(
        "LibreOffice (libreoffice / soffice) is not on PATH. "
        "Install it on the build agent: "
        "Linux: `apt-get install libreoffice` / `dnf install libreoffice`. "
        "macOS: `brew install --cask libreoffice`. "
        "Windows: download from libreoffice.org. "
        "Without LibreOffice, non-PDF files (.docx/.pptx/.xlsx) will be "
        "indexed for text + tables only -- no figure extraction."
    )


def is_available() -> bool:
    """True if LibreOffice can be found. Used for graceful fallback."""
    try:
        _libreoffice_binary()
        return True
    except ConverterNotAvailable:
        return False


def needs_conversion(filename: str) -> bool:
    """True if the file should be converted to PDF before further
    processing. PDFs pass through; everything else gets converted."""
    return not filename.lower().endswith(".pdf")


def convert_to_pdf(filename: str, content: bytes,
                    timeout_s: int = 300) -> bytes:
    """Convert a non-PDF document to PDF bytes. Raises ConverterNotAvailable
    if LibreOffice is not installed; ConversionError on any other failure.

    Returns the input unchanged if it's already a PDF (so callers can
    blindly pipe everything through).
    """
    if not needs_conversion(filename):
        return content

    binary = _libreoffice_binary()  # raises ConverterNotAvailable

    # LibreOffice writes the output beside the input by default; use a
    # temp dir so we don't litter the CWD on the agent.
    with tempfile.TemporaryDirectory(prefix="lo_convert_") as tmpdir:
        suffix = Path(filename).suffix
        if not suffix:
            raise ConversionError(
                f"Cannot convert '{filename}': no file extension. "
                "LibreOffice needs the suffix to choose the right reader."
            )
        # Use a stable input filename so the output filename is predictable.
        input_path = Path(tmpdir) / f"input{suffix}"
        input_path.write_bytes(content)

        # `--outdir` puts the output PDF in the same temp dir.
        # `-env:UserInstallation` isolates per-invocation profile so we
        # don't collide with other LibreOffice instances on the agent
        # (each instance writes a profile to ~/.config; concurrent runs
        # without isolation can deadlock).
        profile_dir = Path(tmpdir) / "profile"
        cmd = [
            binary, "--headless",
            "-env:UserInstallation=file:///" + str(profile_dir).replace("\\", "/"),
            "--convert-to", "pdf",
            "--outdir", tmpdir,
            str(input_path),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout_s, shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ConversionError(
                f"LibreOffice timed out after {timeout_s}s on '{filename}'. "
                f"The file may be malformed or extremely large."
            ) from exc

        if result.returncode != 0:
            raise ConversionError(
                f"LibreOffice exit {result.returncode} on '{filename}': "
                f"{(result.stderr or result.stdout or '')[:500]}"
            )

        # LibreOffice names the output `<input-stem>.pdf`.
        output_path = Path(tmpdir) / "input.pdf"
        if not output_path.exists() or output_path.stat().st_size == 0:
            # Try wildcard: some versions name oddly when input has unicode
            candidates = list(Path(tmpdir).glob("*.pdf"))
            if candidates:
                output_path = candidates[0]
            else:
                raise ConversionError(
                    f"LibreOffice ran but produced no PDF for '{filename}'. "
                    f"stderr: {(result.stderr or '')[:300]}"
                )

        out_bytes = output_path.read_bytes()
        if not out_bytes:
            raise ConversionError(
                f"LibreOffice produced an empty PDF for '{filename}'."
            )
        return out_bytes
