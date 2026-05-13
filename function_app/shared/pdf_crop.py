"""
Crop figure regions from a PDF using PyMuPDF (fitz).

DI's bounding polygons are returned in INCHES against the page. PyMuPDF
operates in points (1 inch = 72 points). We render the cropped region as a
PNG at 300 DPI for the vision model to capture fine labels and part numbers.
"""

import base64

import fitz  # PyMuPDF

RENDER_DPI = 300
INCH_TO_PT = 72.0


class CorruptPdfError(Exception):
    """PyMuPDF could not open / parse the PDF (corrupted bytes)."""


class EncryptedPdfError(Exception):
    """PDF is password-protected. We don't try to decrypt; caller should
    fail loud so the operator knows to remove protection upstream."""


def _polygon_bbox_inches(polygon: list[float]) -> tuple[float, float, float, float]:
    """
    DI returns polygons as a flat list [x1,y1,x2,y2,...] in inches.
    Returns (x0, y0, x1, y1) in inches.
    """
    xs = polygon[0::2]
    ys = polygon[1::2]
    return (min(xs), min(ys), max(xs), max(ys))


def _open_pdf(pdf_bytes: bytes):
    """Open a PDF, raising clear errors for corrupt and encrypted inputs.

    PyMuPDF raises a generic RuntimeError for both cases with messages
    like "cannot open broken document" or "cannot authenticate password";
    we map those to dedicated exceptions so callers can branch
    intelligently (e.g. skip + log encrypted, halt + investigate corrupt).
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except RuntimeError as exc:
        msg = str(exc).lower()
        if "password" in msg or "encrypt" in msg or "authenticate" in msg:
            raise EncryptedPdfError(str(exc)) from exc
        raise CorruptPdfError(str(exc)) from exc
    if getattr(doc, "needs_pass", False) or getattr(doc, "is_encrypted", False):
        # Some encrypted PDFs open without raising but refuse to render
        # without authenticate(). Treat as encrypted.
        try:
            doc.close()
        except Exception:
            pass
        raise EncryptedPdfError("PDF is password-protected")
    return doc


def crop_figure_png_b64(
    pdf_bytes: bytes,
    page_number_1based: int,
    polygon_inches: list[float],
    pad_inches: float = 0.05,
) -> tuple[str, dict[str, float]]:
    """
    Render the cropped figure region as a base64 PNG. Returns (b64, bbox_dict).
    bbox_dict is a JSON-serializable description of the figure location:
      {page, x_in, y_in, w_in, h_in}

    Raises CorruptPdfError or EncryptedPdfError for unrenderable PDFs.
    """
    doc = _open_pdf(pdf_bytes)
    try:
        page_index = page_number_1based - 1
        if page_index < 0 or page_index >= doc.page_count:
            raise ValueError(f"page {page_number_1based} out of range (doc has {doc.page_count})")
        page = doc.load_page(page_index)

        x0_in, y0_in, x1_in, y1_in = _polygon_bbox_inches(polygon_inches)
        x0_in -= pad_inches
        y0_in -= pad_inches
        x1_in += pad_inches
        y1_in += pad_inches

        x0_pt = max(0.0, x0_in * INCH_TO_PT)
        y0_pt = max(0.0, y0_in * INCH_TO_PT)
        x1_pt = min(page.rect.width, x1_in * INCH_TO_PT)
        y1_pt = min(page.rect.height, y1_in * INCH_TO_PT)

        # Guard against inverted rectangles (DI polygon with unexpected
        # winding) so fitz.Rect does not raise on zero/negative area.
        if x1_pt <= x0_pt or y1_pt <= y0_pt:
            raise ValueError(
                f"figure rect out of page bounds after clipping "
                f"(x:{x0_pt:.1f}-{x1_pt:.1f}, y:{y0_pt:.1f}-{y1_pt:.1f})"
            )

        clip = fitz.Rect(x0_pt, y0_pt, x1_pt, y1_pt)
        zoom = RENDER_DPI / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
        png_bytes = pix.tobytes("png")
        b64 = base64.b64encode(png_bytes).decode("ascii")

        # Report the rendered region (post-clipping) so downstream consumers
        # can trust bbox as a description of the actual image content.
        bbox = {
            "page": page_number_1based,
            "x_in": round(x0_pt / INCH_TO_PT, 4),
            "y_in": round(y0_pt / INCH_TO_PT, 4),
            "w_in": round((x1_pt - x0_pt) / INCH_TO_PT, 4),
            "h_in": round((y1_pt - y0_pt) / INCH_TO_PT, 4),
        }
        return b64, bbox
    finally:
        doc.close()
