"""
Detect bank statement format from PDF text using keyword matching.

Compares page text against each bank profile's keywords and returns
the best-matching bank_id.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.parsing.config_loader import list_bank_profiles

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def detect_bank(
    page_texts: dict[int, str],
    profiles: list[dict] | None = None,
    max_pages: int = 3,
) -> str | None:
    """
    Infer bank from PDF page text using keyword counts.

    Args:
        page_texts: Dict mapping page_number -> extracted text
        profiles: List of bank profiles (default: load from config)
        max_pages: Max pages to consider for detection

    Returns:
        bank_id of best match, or None if no confident match
    """
    if profiles is None:
        profiles = list_bank_profiles(enabled_only=True)

    if not profiles:
        logger.warning("No bank profiles available for detection")
        return None

    # Combine text from first N pages
    combined = ""
    for i in range(1, max_pages + 1):
        if i in page_texts:
            combined += " " + (page_texts[i] or "")

    combined = combined.lower()

    best_bank_id: str | None = None
    best_count = 0

    for profile in profiles:
        keywords = profile.get("keywords", [])
        if not keywords:
            continue
        count = sum(1 for k in keywords if k.lower() in combined)
        if count > best_count:
            best_count = count
            best_bank_id = profile.get("bank_id")

    if best_bank_id is None or best_count == 0:
        logger.warning("Could not detect bank from PDF text")
        return None

    logger.info("Detected bank: %s (keyword matches: %d)", best_bank_id, best_count)
    return best_bank_id
