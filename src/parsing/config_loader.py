"""
Load and validate bank profile configs from config/bank_profiles/.

Bank profiles define parsing rules (keywords, markers, regex) for each
supported bank statement format (RBC, Scotiabank).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Project root: src/parsing/config_loader.py -> project root is parent of src
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BANK_PROFILES_DIR = PROJECT_ROOT / "config" / "bank_profiles"

_REQUIRED_KEYS = [
    "bank_id",
    "display_name",
    "keywords",
    "start_marker",
    "end_marker",
    "transaction_line_regex",
    "default_currency",
    "statement_metadata",
    "transaction_fields",
]


def _load_profile(bank_id: str) -> dict | None:
    """Load a single bank profile from JSON."""
    path = BANK_PROFILES_DIR / f"{bank_id}.json"
    if not path.exists():
        logger.warning("Bank profile not found: %s", path)
        return None
    try:
        with open(path, encoding="utf-8") as f:
            profile = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load bank profile %s: %s", path, e)
        return None
    return profile


def _validate_profile(profile: dict) -> bool:
    """Validate that profile has required keys."""
    missing = [k for k in _REQUIRED_KEYS if k not in profile]
    if missing:
        logger.warning("Bank profile missing keys: %s", missing)
        return False
    return True


def get_bank_profile(bank_id: str) -> dict | None:
    """
    Load and return the bank profile for the given bank_id.

    Returns None if profile not found or invalid.
    """
    profile = _load_profile(bank_id)
    if profile is None:
        return None
    if not _validate_profile(profile):
        return None
    return profile


def list_bank_profiles(enabled_only: bool = False) -> list[dict]:
    """
    List all available bank profiles.

    If enabled_only is True, only return profiles with enabled=true.
    """
    profiles = []
    if not BANK_PROFILES_DIR.exists():
        logger.warning("Bank profiles directory not found: %s", BANK_PROFILES_DIR)
        return profiles

    for path in BANK_PROFILES_DIR.glob("*.json"):
        bank_id = path.stem
        profile = _load_profile(bank_id)
        if profile is None or not _validate_profile(profile):
            continue
        if enabled_only and not profile.get("enabled", True):
            continue
        profiles.append(profile)

    return profiles
