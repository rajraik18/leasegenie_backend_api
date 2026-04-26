"""PDF OCR service — Tier 1 upgraded.

Strategy (per page):
    1. Extract with pdfplumber (fast, works for digital PDFs).
    2. Score the extracted text for OCR-quality defects (garbled glyphs,
       suspicious $-amount corruption, missing-space runs).
    3. If the page is EMPTY or CORRUPTED, run a second extraction using
       OCR (PaddleOCR preferred, Tesseract fallback).
    4. Pick the cleaner of the two outputs.

The old behaviour only triggered OCR when the page was EMPTY (<30 chars).
We now detect _garbled_ text layers too — the root cause of the Sample 1
corruption (e.g. "$683.57" extracted as "w").

Produces a list of PageText objects with page number, raw text, text source,
and a quality score.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pdfplumber

logger = logging.getLogger(__name__)

# -- OCR backend availability --
_PADDLE_AVAILABLE = False
_TESSERACT_AVAILABLE = False

try:
    from paddleocr import PaddleOCR  # type: ignore
    _PADDLE_AVAILABLE = True
    _paddle_singleton: Optional["PaddleOCR"] = None
    _paddle_lock = __import__("threading").Lock()
except ImportError:  # pragma: no cover
    pass

try:
    import pytesseract  # type: ignore
    from PIL import Image  # type: ignore  # noqa: F401
    _TESSERACT_AVAILABLE = True
except ImportError:  # pragma: no cover
    pass


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MIN_DIGITAL_TEXT_CHARS = 30          # below this ⇒ page considered empty
CORRUPTION_THRESHOLD = 0.15          # >15% garbled tokens ⇒ prefer OCR
OCR_MARGIN = 0.10                    # OCR must be ≥10% better to override digital


@dataclass
class PageText:
    page_number: int              # 1-indexed
    text: str
    source: str                   # "digital" | "ocr_paddle" | "ocr_tesseract" | "empty"
    quality_score: float          # 0.0–1.0, higher is better
    digital_score: float          # for debugging
    ocr_score: float              # for debugging (-1 if OCR not run)


@dataclass
class DocumentText:
    filename: str
    pages: list[PageText]

    @property
    def full_text(self) -> str:
        return "\n\n".join(
            f"[Page {p.page_number}]\n{p.text}" for p in self.pages
        )


# ---------------------------------------------------------------------------
# Text quality scoring
# ---------------------------------------------------------------------------

# Corruption signals observed in real leases:
_CORRUPTION_PATTERNS = [
    re.compile(r"[§■□▪●▲]"),                  # any replacement glyph anywhere
    re.compile(r"[a-z]{2}[0-9]{2}[a-z]"),    # mixed-encoding: "55—40n"
    re.compile(r"\b\w\s*—\s*\d"),            # em-dash mid-token
    re.compile(r"\$[^0-9\s\(.]"),            # $ followed by non-digit/paren/dot
    re.compile(r"[a-z]{4,}[A-Z]{2,}[a-z]"),  # missed-space camelCase runs
    re.compile(r"\b[^aeiouAEIOU\s\d][^aeiouAEIOU\s\d]{4,}\b"),  # consonant clusters
]

# Field-context corruption: a label followed by a single orphaned non-numeric token
# e.g. "Common Area Charges: w", "Security Deposit: ■/A"
# Money-field labels we expect to precede numeric values:
_MONEY_FIELD_LABEL = re.compile(
    r"(?:Base Rent|Monthly Base Rent|Security Deposit|Rent|Total|Taxes|Insurance|"
    r"Utilities|Common Area Charges|Operating Expense|Mgt\. Fee|Others?|Amount|"
    r"Allowance|Deposit)\s*[:\.]",
    re.IGNORECASE,
)

# What a valid money value looks like right after the label:
_VALID_MONEY_VALUE = re.compile(
    r"^\s*(?:\$?[\d,]+(?:\.\d{1,2})?|N/?A|TBD|None|See\s+\w+|\d+\.\d+%|[A-Z][a-z]+)",
    re.IGNORECASE,
)

_WORD = re.compile(r"\b[A-Za-z]{2,}\b")


def _count_orphaned_money_values(text: str) -> int:
    """Count places where a money-field label is followed by garbage.

    This catches the Sample 1 corruption where "$683.57" extracted as "w"
    and "$540.68" extracted as "55—40■".
    """
    count = 0
    for m in _MONEY_FIELD_LABEL.finditer(text):
        tail = text[m.end(): m.end() + 60].lstrip()
        if not tail:
            continue
        # Is the next token a valid money-like value?
        if _VALID_MONEY_VALUE.match(tail):
            continue
        # Is it a very short non-word token? (the "w" case)
        first_token = tail.split(None, 1)[0] if tail.split() else ""
        if len(first_token) <= 2 and not first_token[0].isalpha():
            count += 1
        elif len(first_token) <= 2 and first_token.isalpha():
            # A single-letter "value" after a money label = corrupted
            count += 1
        elif re.match(r"^[a-z0-9]*[■§▪]", first_token):
            count += 1
        elif re.match(r"^\d+[—–][\d]+[a-z■]", first_token):
            # "55—40n" or "55—40■" style
            count += 1
    return count


def _quality_score(text: str) -> float:
    """Return a quality score in [0, 1]. Higher is better."""
    if not text or len(text.strip()) < MIN_DIGITAL_TEXT_CHARS:
        return 0.0

    n = len(text)
    clean_chars = sum(
        1 for c in text
        if c.isalnum() or c.isspace() or c in ".,;:()[]-'\"/$%&*@#"
    )
    readability = clean_chars / n

    words = _WORD.findall(text)
    expected_words = n / 6.0
    word_ratio = min(1.0, len(words) / max(1.0, expected_words))

    n_corruption = sum(len(p.findall(text)) for p in _CORRUPTION_PATTERNS)
    # Orphaned money values each count as a strong corruption signal (weight 3)
    n_corruption += 3 * _count_orphaned_money_values(text)
    clean_ratio = max(0.0, 1.0 - (n_corruption / max(1, len(words))))

    return round(0.3 * readability + 0.3 * word_ratio + 0.4 * clean_ratio, 4)


def _is_garbled(text: str) -> bool:
    """Cheap pre-check before running expensive OCR."""
    if len(text.strip()) < MIN_DIGITAL_TEXT_CHARS:
        return True
    words = _WORD.findall(text)
    if not words:
        return True
    # Strong signal: any orphaned value after a money field label
    if _count_orphaned_money_values(text) >= 2:
        return True
    n_corruption = sum(len(p.findall(text)) for p in _CORRUPTION_PATTERNS)
    return (n_corruption / len(words)) > CORRUPTION_THRESHOLD


# ---------------------------------------------------------------------------
# OCR backends
# ---------------------------------------------------------------------------

def _get_paddle():
    """Lazy-load PaddleOCR (heavy; don't load unless needed). Thread-safe."""
    global _paddle_singleton
    if not _PADDLE_AVAILABLE:
        return None
    if _paddle_singleton is not None:
        return _paddle_singleton
    with _paddle_lock:
        if _paddle_singleton is None:
            try:
                _paddle_singleton = PaddleOCR(
                    use_angle_cls=True, lang="en", show_log=False, use_gpu=False,
                )
            except Exception as exc:
                logger.warning("PaddleOCR init failed: %s — falling back to Tesseract", exc)
                return None
    return _paddle_singleton


def _ocr_with_paddle(pil_img) -> str:
    engine = _get_paddle()
    if engine is None:
        return ""
    try:
        import numpy as np  # type: ignore
        arr = np.array(pil_img)
        result = engine.ocr(arr, cls=True)
        lines = []
        if result and result[0]:
            for item in result[0]:
                if item and len(item) >= 2 and item[1]:
                    lines.append(item[1][0])
        return "\n".join(lines)
    except Exception as exc:  # pragma: no cover
        logger.warning("PaddleOCR failed: %s", exc)
        return ""


def _ocr_with_tesseract(pil_img) -> str:
    if not _TESSERACT_AVAILABLE:
        return ""
    try:
        return pytesseract.image_to_string(pil_img)
    except Exception as exc:  # pragma: no cover
        logger.warning("Tesseract failed: %s", exc)
        return ""


def _ocr_page(page, page_number: int) -> tuple[str, str]:
    """Rasterize page and run OCR. Returns (text, source_label)."""
    try:
        img = page.to_image(resolution=250).original
    except Exception as exc:
        logger.warning("Could not rasterize page %d: %s", page_number, exc)
        return "", "empty"

    # Try Paddle first (generally better), then Tesseract
    if _PADDLE_AVAILABLE:
        text = _ocr_with_paddle(img)
        if text and _quality_score(text) > 0.3:
            return text, "ocr_paddle"

    if _TESSERACT_AVAILABLE:
        text = _ocr_with_tesseract(img)
        if text:
            return text, "ocr_tesseract"

    return "", "empty"


# ---------------------------------------------------------------------------
# Main extraction entry point
# ---------------------------------------------------------------------------

def extract_document_text(
    pdf_path: Path,
    force_ocr: bool = False,
    verbose: bool = False,
) -> DocumentText:
    """Extract text from a PDF with parallel-and-score OCR fallback.

    For each page we:
      (1) extract via pdfplumber;
      (2) score the result;
      (3) if empty/garbled OR force_ocr, run OCR and keep the higher-scoring text.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    pages: list[PageText] = []
    ocr_any_available = _PADDLE_AVAILABLE or _TESSERACT_AVAILABLE

    with pdfplumber.open(str(pdf_path)) as pdf:
        # Enforce the configured page-count cap before walking pages so
        # a 50,000-page PDF cannot wedge a worker for hours.
        from app.config import settings as _settings
        cap = _settings.max_pdf_pages
        if cap > 0 and len(pdf.pages) > cap:
            raise ValueError(
                f"PDF has {len(pdf.pages)} pages -- exceeds MAX_PDF_PAGES={cap}"
            )

        for idx, page in enumerate(pdf.pages, start=1):
            try:
                digital_text = page.extract_text() or ""
            except Exception as exc:  # pragma: no cover
                logger.warning("pdfplumber page %d failed: %s", idx, exc)
                digital_text = ""

            digital_score = _quality_score(digital_text)
            ocr_score = -1.0
            ocr_text = ""
            ocr_source = "empty"

            needs_ocr = force_ocr or _is_garbled(digital_text)

            if needs_ocr and ocr_any_available:
                ocr_text, ocr_source = _ocr_page(page, idx)
                ocr_score = _quality_score(ocr_text)

                if ocr_score > digital_score + OCR_MARGIN:
                    chosen_text, chosen_source, chosen_score = (
                        ocr_text, ocr_source, ocr_score
                    )
                else:
                    chosen_text, chosen_source, chosen_score = (
                        digital_text, "digital", digital_score
                    )
            else:
                chosen_text, chosen_source, chosen_score = (
                    digital_text, "digital", digital_score
                )

            if verbose:
                logger.info(
                    "Page %d: digital=%.3f ocr=%.3f chosen=%s",
                    idx, digital_score, ocr_score, chosen_source,
                )

            pages.append(PageText(
                page_number=idx,
                text=chosen_text,
                source=chosen_source,
                quality_score=chosen_score,
                digital_score=digital_score,
                ocr_score=ocr_score,
            ))

    return DocumentText(filename=pdf_path.name, pages=pages)


def document_ocr_summary(doc: DocumentText) -> dict:
    """Return page-level stats useful for debugging & monitoring."""
    by_source: dict[str, int] = {}
    q_sum = 0.0
    low_q_pages = []
    for p in doc.pages:
        by_source[p.source] = by_source.get(p.source, 0) + 1
        q_sum += p.quality_score
        if p.quality_score < 0.5:
            low_q_pages.append(p.page_number)
    return {
        "filename": doc.filename,
        "pages": len(doc.pages),
        "mean_quality": round(q_sum / len(doc.pages), 3) if doc.pages else 0,
        "by_source": by_source,
        "low_quality_pages": low_q_pages,
    }


# ---------------------------------------------------------------------------
# Clause segmentation (unchanged)
# ---------------------------------------------------------------------------

CLAUSE_PATTERNS = [
    re.compile(r"^\s*(\d+(?:\.\d+){0,3})[\.\)]?\s+([A-Z][^\n]{0,200})", re.MULTILINE),
    re.compile(r"^\s*(Section|Article|Clause)\s+([\dIVX]+)[\.:\s]", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*([A-Z][A-Z\s]{10,80})\s*$", re.MULTILINE),
]


@dataclass
class Clause:
    clause_number: str | None
    heading: str | None
    text: str
    page_number: int
    start_offset: int
    end_offset: int


def segment_clauses(doc: DocumentText) -> list[Clause]:
    """Split the document into clause-sized chunks using heading heuristics."""
    clauses: list[Clause] = []
    for page in doc.pages:
        text = page.text
        if not text.strip():
            continue

        marks: list[tuple[int, str | None, str | None]] = []
        for pat in CLAUSE_PATTERNS:
            for m in pat.finditer(text):
                num = m.group(1) if m.lastindex and m.lastindex >= 1 else None
                head = m.group(2) if m.lastindex and m.lastindex >= 2 else None
                marks.append((m.start(), num, head))
        marks.sort(key=lambda x: x[0])

        if not marks:
            clauses.append(Clause(
                clause_number=None, heading=None,
                text=text.strip(), page_number=page.page_number,
                start_offset=0, end_offset=len(text),
            ))
            continue

        if marks[0][0] > 0:
            prefix = text[: marks[0][0]].strip()
            if prefix:
                clauses.append(Clause(
                    clause_number=None, heading=None,
                    text=prefix, page_number=page.page_number,
                    start_offset=0, end_offset=marks[0][0],
                ))

        for i, (offset, num, head) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
            body = text[offset:end].strip()
            if body:
                clauses.append(Clause(
                    clause_number=num, heading=head,
                    text=body, page_number=page.page_number,
                    start_offset=offset, end_offset=end,
                ))
    return clauses
