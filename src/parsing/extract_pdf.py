"""
Extract transaction blocks from bank statement PDFs.

Uses bank profile config for keywords, start/end markers, and applies
line preprocessing (e.g. strip Continuedonpage) before returning blocks.
"""

from __future__ import annotations

import re
import logging
from pathlib import Path
from typing import Any

import pdfplumber

from src.parsing.config_loader import get_bank_profile

logger = logging.getLogger(__name__)


def _preprocess_lines(
    lines: list[str],
    ignore_patterns: list[str],
) -> list[str]:
    """
    Preprocess raw lines: strip ignore patterns (e.g. Continuedonpage3).

    Removes content matching ignore_patterns and splits lines if needed.
    """
    result = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Remove ignore_pattern content (e.g. Continuedonpage3)
        for pat in ignore_patterns:
            line = re.sub(pat, "", line, flags=re.IGNORECASE)
        line = line.strip()
        if line:
            result.append(line)
    return result


def _extract_blocks_from_page(
    page_text: str,
    page_number: int,
    profile: dict[str, Any],
) -> list[str]:
    """Extract transaction lines from a single page."""
    start_marker = profile["start_marker"]
    end_marker = profile["end_marker"]
    ignore_patterns = profile.get("ignore_patterns", [])

    lines = page_text.split("\n")
    start_idx: int | None = None
    end_idx: int | None = None

    # Start marker detection: prioritize literal match, then regex fallback
    start_idx: int | None = None
    use_start_regex = "|" in start_marker or "\\s" in start_marker or "(" in start_marker

    for i, line in enumerate(lines):
        if start_marker in line:
            start_idx = i + 1
            break
        if use_start_regex:
            # Fallback for complex patterns (e.g. RBC's A|B)
            if re.search(start_marker, line):
                start_idx = i + 1
                break

    if start_idx is None:
        logger.warning("Could not find start marker on page %d", page_number)
        return []

    # End marker detection: prioritize literal match, then regex fallback
    end_idx: int | None = None
    use_end_regex = "|" in end_marker or "(" in end_marker

    for j in range(start_idx, len(lines)):
        if end_marker in lines[j]:
            end_idx = j
            break
        if use_end_regex:
             if re.search(end_marker, lines[j]):
                end_idx = j
                break

    if end_idx is None:
        end_idx = len(lines)

    raw_lines = lines[start_idx:end_idx]
    processed = _preprocess_lines(raw_lines, ignore_patterns)
    return processed



def extract_pdf(
    pdf_path: str | Path,
    bank_id: str | None = None,
) -> tuple[dict[int, str], list[dict[str, Any]], str | None]:
    """
    Extract pages and transaction blocks from a bank statement PDF.

    Args:
        pdf_path: Path to PDF file
        bank_id: Optional bank_id; if None, bank is auto-detected

    Returns:
        (pages_text, transaction_blocks, bank_id)
        pages_text: {page_number: text}
        transaction_blocks: [{"page_number": N, "lines": [...]}, ...]
        bank_id: detected or provided bank_id, or None on failure
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        logger.error("PDF not found: %s", pdf_path)
        return {}, [], None

    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages_text: dict[int, str] = {}
            for i, page in enumerate(pdf.pages, start=1):
                pages_text[i] = page.extract_text() or ""
    except Exception as e:
        logger.error("Failed to open PDF %s: %s", pdf_path, e)
        return {}, [], None

    # Detect or validate bank
    if bank_id is None:
        from src.parsing.bank_detector import detect_bank
        bank_id = detect_bank(pages_text)

    if bank_id is None:
        logger.error("Could not determine bank for PDF")
        return pages_text, [], None

    profile = get_bank_profile(bank_id)
    if profile is None:
        logger.error("Bank profile not found: %s", bank_id)
        return pages_text, [], None

    # Find pages with transaction content
    keywords = profile.get("keywords", [])
    pages_to_parse: list[int] = []
    for page_num, text in pages_text.items():
        if any(k in text for k in keywords):
            pages_to_parse.append(page_num)

    logger.info("Pages to parse (1-based): %s", pages_to_parse)

    transaction_blocks: list[dict[str, Any]] = []
    for page_num in pages_to_parse:
        text = pages_text.get(page_num, "")
        lines = _extract_blocks_from_page(text, page_num, profile)
        transaction_blocks.append({"page_number": page_num, "lines": lines})
        logger.info("Extracted %d transaction lines from page %d", len(lines), page_num)

    return pages_text, transaction_blocks, bank_id
