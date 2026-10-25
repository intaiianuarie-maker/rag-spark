"""Compatibility API; concrete SDK implementation lives behind adapters."""

import sys

from .adapters import pdf_ocr as _implementation
from .adapters.pdf_ocr import (
    classify_page as classify_page,
)
from .adapters.pdf_ocr import (
    classify_pdf_pages as classify_pdf_pages,
)
from .adapters.pdf_ocr import (
    process_pdf_with_ocr as process_pdf_with_ocr,
)

# Preserve module identity for dependency injection and existing callers.
sys.modules[__name__] = _implementation
