"""PDFium-based page rasterization adapter (PD-001, Apache-2.0 / BSD-3-Clause).

Renders PDF pages to raster images (PNG) at specified DPI (default 300 DPI)
and provides coordinate transforms from raster pixels back to original PDF
point dimensions (72 points/inch, origin top-left).
"""

from __future__ import annotations

from io import BytesIO

import pypdfium2 as pdfium

from ..interfaces import BBox

DEFAULT_DPI: int = 300
MAX_PAGE_PIXELS: int = 25_000_000


def transform_bbox_to_points(
    left: float,
    top: float,
    width: float,
    height: float,
    *,
    pixel_width: int,
    pixel_height: int,
    width_pts: float,
    height_pts: float,
) -> BBox:
    """Transform pixel bounding box to PDF points (origin top-left)."""
    if pixel_width <= 0 or pixel_height <= 0:
        return BBox(x0=0.0, y0=0.0, x1=0.0, y1=0.0)

    scale_x = width_pts / pixel_width
    scale_y = height_pts / pixel_height

    x0 = left * scale_x
    y0 = top * scale_y
    x1 = (left + width) * scale_x
    y1 = (top + height) * scale_y

    return BBox(
        x0=round(x0, 2),
        y0=round(y0, 2),
        x1=round(x1, 2),
        y1=round(y1, 2),
    )


def get_pdf_page_count(data: bytes) -> int:
    """Get total page count of a PDF using pdfium."""
    doc = pdfium.PdfDocument(data)
    return len(doc)


def render_pdf_page(
    data: bytes,
    page_number: int,
    *,
    dpi: int = DEFAULT_DPI,
    max_pixels: int = MAX_PAGE_PIXELS,
) -> tuple[bytes, float, float, int, int]:
    """Render a 1-indexed PDF page to PNG bytes at specified DPI.

    Returns:
        tuple of (png_bytes, width_pts, height_pts, pixel_width, pixel_height)
    """
    if page_number < 1:
        raise ValueError(f"page_number must be >= 1, got {page_number}")

    doc = pdfium.PdfDocument(data)
    if page_number > len(doc):
        raise ValueError(f"page_number {page_number} exceeds total pages {len(doc)}")

    page = doc[page_number - 1]
    width_pts, height_pts = page.get_size()

    scale = dpi / 72.0
    expected_w = int(width_pts * scale)
    expected_h = int(height_pts * scale)
    if expected_w * expected_h > max_pixels:
        raise ValueError(
            f"Page {page_number} pixel count ({expected_w * expected_h}) "
            f"exceeds budget ({max_pixels})"
        )

    bitmap = page.render(scale=scale)
    pil_image = bitmap.to_pil()
    buf = BytesIO()
    pil_image.save(buf, format="PNG")

    return (
        buf.getvalue(),
        float(width_pts),
        float(height_pts),
        pil_image.width,
        pil_image.height,
    )
