"""
LLM-based merchant standardization client.

Uses Groq (free tier) as the default provider, with optional fallback to
Google Gemini or OpenAI. Results are cached to disk to avoid redundant API calls.

Usage:
    from src.llm_client import standardize_merchant_with_llm
    name, confidence = standardize_merchant_with_llm("AMZN MKTP CA*2M4N7P3 ON")
    # → ("Amazon", 0.75)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / ".llm_cache"

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

# Valid category taxonomy — must match the dbt Gold layer exactly.
_VALID_CATEGORIES = [
    "Groceries",
    "Telecommunications",
    "Rideshare & Food Delivery",
    "Professional Development",
    "Education & Learning",
    "Online Retail",
    "Retail",
    "Software & Services",
    "Office & Printing",
    "Food & Dining",
    "Health & Pharmacy",
    "Entertainment & Events",
    "Travel & Accommodation",
    "Transportation",
    "Internal Payment",
    "Uncategorized",
]

_SYSTEM_PROMPT = (
    "You are a financial transaction classifier for a Canadian personal expense tracker. "
    "Given a raw bank transaction description, extract three fields and return ONLY valid JSON. "
    "Fields:\n"
    "  - merchant_standardized: canonical merchant name (title case, no store numbers, city codes, or transaction IDs)\n"
    "  - category: one of the valid categories listed in the prompt — do NOT invent new categories\n"
    "  - subcategory: a concise descriptive sub-type (e.g. 'Streaming', 'Gas Station', 'Restaurant')\n"
    "If the description is a bank payment, credit, or internal transfer, use category 'Internal Payment'. "
    "Output ONLY the JSON object, no explanation, no markdown fences."
)

_FEW_SHOT_EXAMPLES = [
    (
        "AMZN MKTP CA*2M4N7P3 866-216-1072 ON",
        '{"merchant_standardized": "Amazon", "category": "Online Retail", "subcategory": "Marketplace"}',
    ),
    (
        "TIM HORTONS #9382 TORONTO ON",
        '{"merchant_standardized": "Tim Hortons", "category": "Food & Dining", "subcategory": "Coffee & Fast Food"}',
    ),
    (
        "UBER CANADA/UBERTRIP TORONTO ON",
        '{"merchant_standardized": "Uber", "category": "Rideshare & Food Delivery", "subcategory": "Rideshare"}',
    ),
    (
        "PAYMENT - THANK YOU / PAIEMENT - MERCI",
        '{"merchant_standardized": "Internal Payment", "category": "Internal Payment", "subcategory": "Credit Card Payment"}',
    ),
    (
        "NETFLIX.COM NETFLIX.COM CA",
        '{"merchant_standardized": "Netflix", "category": "Entertainment & Events", "subcategory": "Streaming"}',
    ),
    (
        "LINKEDINPREA *95211646 MOUNTAIN VIEWCA",
        '{"merchant_standardized": "LinkedIn", "category": "Professional Development", "subcategory": "Online Learning & Career"}',
    ),
    (
        "SHOPPERS DRUG MART #2847 TORONTO ON",
        '{"merchant_standardized": "Shoppers Drug Mart", "category": "Health & Pharmacy", "subcategory": "Pharmacy"}',
    ),
    (
        "PETRO-CANADA #8473 LONDON ON",
        '{"merchant_standardized": "Petro-Canada", "category": "Transportation", "subcategory": "Gas Station"}',
    ),
    (
        "ROGERS WIRELESS VEST ON",
        '{"merchant_standardized": "Rogers", "category": "Telecommunications", "subcategory": "Mobile Provider"}',
    ),
    (
        "GOOGLE *GOOGLE STORAGE G.CO/HELPPAY# CA",
        '{"merchant_standardized": "Google One", "category": "Software & Services", "subcategory": "Cloud Storage"}',
    ),
]


def _build_prompt(merchant_raw: str) -> str:
    """Build a structured few-shot prompt instructing the LLM to return JSON with merchant, category, subcategory."""
    valid_cats = ", ".join(f'"{c}"' for c in _VALID_CATEGORIES)
    examples_text = "\n".join(
        f'Input: "{raw}" -> Output: {canonical}'
        for raw, canonical in _FEW_SHOT_EXAMPLES
    )
    return (
        f"Valid categories: [{valid_cats}]\n\n"
        f"{examples_text}\n"
        f'Input: "{merchant_raw}" -> Output:'
    )


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

def _call_groq(prompt: str, model: str = "llama-3.3-70b-versatile") -> str:
    """Call Groq API (free tier, fast). Uses llama-3.3-70b for better instruction following."""
    from groq import Groq

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY environment variable not set.")

    client = Groq(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=80,  # Increased to accommodate JSON object response
        stop=["\nInput:"],  # Stop at next example marker only
    )
    return response.choices[0].message.content.strip()


def _call_gemini(prompt: str) -> str:
    """Call Google Gemini Flash API (cheapest paid option)."""
    import google.generativeai as genai

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise EnvironmentError("GOOGLE_API_KEY environment variable not set.")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        "gemini-1.5-flash",
        system_instruction=_SYSTEM_PROMPT,
    )
    response = model.generate_content(
        prompt,
        generation_config={"temperature": 0.0, "max_output_tokens": 30},
    )
    return response.text.strip().strip('"').strip("'")


def _call_openai(prompt: str) -> str:
    """Call OpenAI GPT-4o-mini (best quality)."""
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY environment variable not set.")

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=30,
    )
    return response.choices[0].message.content.strip().strip('"').strip("'")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def standardize_merchant_with_llm(
    merchant_raw: str,
    provider: str = "groq",
    use_cache: bool = True,
) -> tuple[str, str, str, float]:
    """
    Use an LLM to classify a raw bank transaction description.

    Args:
        merchant_raw: Raw merchant string from bank statement.
        provider: LLM provider - "groq" (free), "gemini", or "openai".
        use_cache: If True, cache results to disk to avoid repeat API calls.

    Returns:
        (merchant_standardized, category, subcategory, confidence_score)
        confidence_score is always 0.75 for LLM results (lower than catalog=0.95).
    """
    import json as _json

    _FALLBACK = (merchant_raw, "Uncategorized", "Unknown", 0.0)

    if not merchant_raw or not merchant_raw.strip():
        return _FALLBACK

    merchant_raw = merchant_raw.strip()

    # --- Cache lookup ---
    cache = None
    cache_key = f"{provider}:v2:{merchant_raw}"  # v2 key because response shape changed
    if use_cache:
        try:
            from diskcache import Cache
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache = Cache(str(CACHE_DIR))
            if cache_key in cache:
                cached = cache[cache_key]
                logger.debug("LLM cache hit for '%s'", merchant_raw)
                return cached
        except Exception as e:
            logger.warning("Cache read failed: %s", e)

    # --- Build prompt and call LLM ---
    prompt = _build_prompt(merchant_raw)
    try:
        if provider == "groq":
            raw_response = _call_groq(prompt)
        elif provider == "gemini":
            raw_response = _call_gemini(prompt)
        elif provider == "openai":
            raw_response = _call_openai(prompt)
        else:
            raise ValueError(f"Unknown LLM provider: '{provider}'. Use 'groq', 'gemini', or 'openai'.")

        logger.debug("LLM raw response for '%s': %s (via %s)", merchant_raw, raw_response, provider)

    except Exception as e:
        logger.warning("LLM call failed for '%s': %s", merchant_raw, e)
        return _FALLBACK

    # --- Parse JSON response ---
    try:
        # Strip accidental markdown fences if model adds them
        clean = raw_response.strip().strip('`').strip()
        if clean.startswith("json"):
            clean = clean[4:].strip()
        parsed = _json.loads(clean)
        merchant_std = parsed.get("merchant_standardized", merchant_raw).strip().strip('"').strip("'")
        category = parsed.get("category", "Uncategorized").strip()
        subcategory = parsed.get("subcategory", "Unknown").strip()

        # Guard: if LLM invented a category, fall back to Uncategorized
        if category not in _VALID_CATEGORIES:
            logger.warning("LLM returned invalid category '%s' for '%s'; defaulting to Uncategorized", category, merchant_raw)
            category = "Uncategorized"

        result = (merchant_std, category, subcategory, 0.75)

    except (_json.JSONDecodeError, AttributeError) as e:
        logger.warning("LLM JSON parse failed for '%s' (response: %r): %s", merchant_raw, raw_response, e)
        # Best effort: treat the whole response as a merchant name, no category
        result = (raw_response.strip('"').strip("'"), "Uncategorized", "Unknown", 0.5)

    # --- Cache result ---
    if use_cache and cache is not None:
        try:
            cache[cache_key] = result
        except Exception as e:
            logger.warning("Cache write failed: %s", e)

    return result


def batch_standardize_merchants(
    merchants: list[str],
    provider: str = "groq",
    use_cache: bool = True,
) -> list[tuple[str, str, str, float]]:
    """
    Classify a batch of raw merchant strings using LLM.

    Processes sequentially (Groq free tier: 30 req/min).
    Returns list of (merchant_standardized, category, subcategory, confidence) tuples.
    """
    results = []
    for merchant in merchants:
        result = standardize_merchant_with_llm(merchant, provider=provider, use_cache=use_cache)
        results.append(result)
    return results
