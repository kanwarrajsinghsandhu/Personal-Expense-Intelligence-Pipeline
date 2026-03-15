"""
Bank profile configs for statement parsing (RBC, Scotiabank).

This package holds JSON profiles that define parsing rules per bank.
Use PROFILE_DIR and available_bank_ids() to discover profiles;
actual loading/validation is done by src.parsing.config_loader.
"""

from pathlib import Path

# Directory containing rbc.json, scotia.json, etc.
PROFILE_DIR = Path(__file__).resolve().parent


def available_bank_ids() -> list[str]:
    """Return list of bank_id values (from *.json filenames in this directory)."""
    return [
        p.stem
        for p in PROFILE_DIR.glob("*.json")
    ]


def get_profile_path(bank_id: str) -> Path:
    """Return path to the JSON file for the given bank_id."""
    return PROFILE_DIR / f"{bank_id}.json"
