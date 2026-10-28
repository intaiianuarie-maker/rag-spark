"""Tesseract OCR adapter (PD-001 / Apache-2.0).

Invokes Tesseract 5 CLI subprocess with `-l <languages> --oem 1 tsv` to extract
words, line structures, confidence scores, and bounding boxes, returning
project-owned normalized contracts without leaking third-party types.
"""

from __future__ import annotations

import csv
import os
import subprocess
from io import StringIO
from typing import Any

from ..interfaces import BBox, DocumentElement, ElementType, OcrPageResult, OcrWord
from .pdfium_render import transform_bbox_to_points

DEFAULT_LANGUAGES = "ron+eng+por"
DEFAULT_PSM = 3
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_OMP_THREAD_LIMIT = "2"


class OcrError(Exception):
    """Base error for OCR operations."""


class OcrTimeoutError(OcrError):
    """Raised when OCR subprocess execution exceeds the deadline."""


class OcrExecutionError(OcrError):
    """Raised when the OCR subprocess fails."""


def get_tesseract_version() -> str:
    """Return the installed Tesseract version string."""
    try:
        proc = subprocess.run(
            ["tesseract", "--version"],
            capture_output=True,
            text=True,
            check=True,
        )
        line = proc.stdout.splitlines()[0] if proc.stdout else ""
        return line.strip()
    except Exception:
        return "tesseract 5.x"


def parse_tsv_words(
    tsv_content: str,
    *,
    width_pts: float,
    height_pts: float,
    pixel_width: int,
    pixel_height: int,
) -> list[OcrWord]:
    """Parse Tesseract TSV output into a list of OcrWord objects."""
    words: list[OcrWord] = []
    reader = csv.reader(StringIO(tsv_content), delimiter="\t")
    header = next(reader, None)
    if not header:
        return []

    # Map column names to indices
    col_idx: dict[str, int] = {name: i for i, name in enumerate(header)}
    level_idx = col_idx.get("level", 0)
    block_idx = col_idx.get("block_num", 2)
    par_idx = col_idx.get("par_num", 3)
    line_idx = col_idx.get("line_num", 4)
    word_idx = col_idx.get("word_num", 5)
    left_idx = col_idx.get("left", 6)
    top_idx = col_idx.get("top", 7)
    width_idx = col_idx.get("width", 8)
    height_idx = col_idx.get("height", 9)
    conf_idx = col_idx.get("conf", 10)
    text_idx = col_idx.get("text", 11)

    for row in reader:
        if len(row) <= text_idx:
            continue
        try:
            level = int(row[level_idx])
            # level 5 indicates word level in Tesseract TSV
            if level != 5:
                continue

            raw_text = row[text_idx].strip()
            if not raw_text:
                continue

            conf = float(row[conf_idx])
            if conf < 0:
                continue

            left = float(row[left_idx])
            top = float(row[top_idx])
            w = float(row[width_idx])
            h = float(row[height_idx])

            bbox = transform_bbox_to_points(
                left,
                top,
                w,
                h,
                pixel_width=pixel_width,
                pixel_height=pixel_height,
                width_pts=width_pts,
                height_pts=height_pts,
            )

            words.append(
                OcrWord(
                    text=raw_text,
                    confidence=round(conf, 2),
                    bbox=bbox,
                    line_num=int(row[line_idx]),
                    block_num=int(row[block_idx]),
                    par_num=int(row[par_idx]),
                    word_num=int(row[word_idx]),
                )
            )
        except (ValueError, IndexError):
            continue

    return words


def build_line_elements(
    words: list[OcrWord],
    *,
    page_number: int,
    engine_version: str,
    languages: str,
) -> list[DocumentElement]:
    """Group words into DocumentElements by (block_num, par_num, line_num)."""
    if not words:
        return []

    lines_map: dict[tuple[int, int, int], list[OcrWord]] = {}
    for word in words:
        key = (word.block_num, word.par_num, word.line_num)
        lines_map.setdefault(key, []).append(word)

    elements: list[DocumentElement] = []
    for _key, line_words in lines_map.items():
        line_text = " ".join(w.text for w in line_words).strip()
        if not line_text:
            continue

        mean_conf = sum(w.confidence for w in line_words) / len(line_words)
        x0 = min(w.bbox.x0 for w in line_words)
        y0 = min(w.bbox.y0 for w in line_words)
        x1 = max(w.bbox.x1 for w in line_words)
        y1 = max(w.bbox.y1 for w in line_words)
        line_bbox = BBox(x0=round(x0, 2), y0=round(y0, 2), x1=round(x1, 2), y1=round(y1, 2))

        source_spans: list[dict[str, Any]] = [
            {
                "word": w.text,
                "confidence": w.confidence,
                "bbox": [w.bbox.x0, w.bbox.y0, w.bbox.x1, w.bbox.y1],
            }
            for w in line_words
        ]

        elements.append(
            DocumentElement(
                type=ElementType.TEXT,
                text=line_text,
                page=page_number,
                section=None,
                bbox=line_bbox,
                metadata={
                    "extraction": {
                        "method": "tesseract",
                        "engine_version": engine_version,
                        "requested_languages": languages.split("+"),
                        "page": page_number,
                        "confidence": round(mean_conf, 2),
                        "source_spans": source_spans,
                    }
                },
            )
        )

    return elements


def ocr_page_image(
    png_bytes: bytes,
    *,
    page_number: int,
    width_pts: float,
    height_pts: float,
    pixel_width: int,
    pixel_height: int,
    languages: str = DEFAULT_LANGUAGES,
    psm: int = DEFAULT_PSM,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> OcrPageResult:
    """Run Tesseract CLI on raster image bytes and return an OcrPageResult."""
    env = dict(os.environ)
    env["OMP_THREAD_LIMIT"] = DEFAULT_OMP_THREAD_LIMIT

    cmd = [
        "tesseract",
        "stdin",
        "stdout",
        "-l",
        languages,
        "--oem",
        "1",
        "--psm",
        str(psm),
        "tsv",
    ]

    try:
        proc = subprocess.run(
            cmd,
            input=png_bytes,
            capture_output=True,
            env=env,
            timeout=timeout_seconds,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrTimeoutError(
            f"Tesseract OCR timed out after {timeout_seconds}s on page {page_number}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr_text = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        raise OcrExecutionError(
            f"Tesseract OCR failed on page {page_number} with code {exc.returncode}: {stderr_text}"
        ) from exc
    except Exception as exc:
        raise OcrExecutionError(
            f"Failed to invoke Tesseract OCR on page {page_number}: {exc}"
        ) from exc

    tsv_output = proc.stdout.decode("utf-8", errors="replace")
    words = parse_tsv_words(
        tsv_output,
        width_pts=width_pts,
        height_pts=height_pts,
        pixel_width=pixel_width,
        pixel_height=pixel_height,
    )

    engine_version = get_tesseract_version()
    elements = build_line_elements(
        words,
        page_number=page_number,
        engine_version=engine_version,
        languages=languages,
    )

    full_text = "\n".join(el.text for el in elements)
    mean_conf = (sum(w.confidence for w in words) / len(words)) if words else 0.0

    return OcrPageResult(
        page_number=page_number,
        text=full_text,
        words=words,
        mean_confidence=round(mean_conf, 2),
        width_pts=width_pts,
        height_pts=height_pts,
        elements=elements,
        extraction_method="tesseract",
        engine_version=engine_version,
    )
