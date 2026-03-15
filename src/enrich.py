"""
Enrich parsed transactions with ML-based category/subcategory and optional catalog matching.

Loads trained classifiers and encoders from models/, vectorizes merchant text with
SentenceTransformer, predicts category/subcategory, and optionally overlays
merchant catalog for canonical names and higher-confidence labels.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS_DIR = PROJECT_ROOT / "models"
DEFAULT_CATALOG_PATH = PROJECT_ROOT / "data" / "merchant_catalog.json"


def load_classification_models(models_dir: Path | str | None = None):
    """
    Load SentenceTransformer, category/subcategory classifiers, and label encoders.

    Returns:
        dict with keys: embedder, clf_category, clf_subcategory, enc_category, enc_subcategory
    """
    import joblib
    from sentence_transformers import SentenceTransformer

    models_dir = Path(models_dir or DEFAULT_MODELS_DIR)
    if not models_dir.exists():
        raise FileNotFoundError(f"Models directory not found: {models_dir}")

    logger.info("Loading classification models from %s", models_dir)
    embedder = SentenceTransformer("all-MiniLM-L6-v2")
    clf_category = joblib.load(models_dir / "category_classifier.pkl")
    clf_subcategory = joblib.load(models_dir / "subcategory_classifier.pkl")
    enc_category = joblib.load(models_dir / "category_encoder.pkl")
    enc_subcategory = joblib.load(models_dir / "subcategory_encoder.pkl")

    return {
        "embedder": embedder,
        "clf_category": clf_category,
        "clf_subcategory": clf_subcategory,
        "enc_category": enc_category,
        "enc_subcategory": enc_subcategory,
    }


def predict_categories_ml(
    df: pd.DataFrame,
    models: dict[str, Any],
    text_column: str = "merchant",
) -> pd.DataFrame:
    """
    Add category and subcategory columns using trained ML models.

    Vectorizes text_column with SentenceTransformer, runs classifiers, decodes labels.
    Adds: category, subcategory, match_type='ml'
    """
    if df.empty or text_column not in df.columns:
        df = df.copy()
        df["category"] = pd.NA
        df["subcategory"] = pd.NA
        df["match_type"] = "ml"
        return df

    df = df.copy()
    embedder = models["embedder"]
    clf_cat = models["clf_category"]
    clf_sub = models["clf_subcategory"]
    enc_cat = models["enc_category"]
    enc_sub = models["enc_subcategory"]

    # Fill NaN merchant with placeholder so embedder doesn't fail
    texts = df[text_column].fillna("").astype(str).tolist()
    X = embedder.encode(texts, show_progress_bar=False)

    pred_cat = clf_cat.predict(X)
    pred_sub = clf_sub.predict(X)
    df["category"] = enc_cat.inverse_transform(pred_cat)
    df["subcategory"] = enc_sub.inverse_transform(pred_sub)
    df["match_type"] = "ml"
    return df


def load_merchant_catalog(catalog_path: Path | str | None = None) -> list[dict]:
    """Load merchant catalog JSON; return list of merchant dicts."""
    path = Path(catalog_path or DEFAULT_CATALOG_PATH)
    if not path.exists():
        logger.warning("Merchant catalog not found: %s", path)
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("merchants", [])


def normalize_merchant(text: str) -> str:
    """
    Normalize merchant text for better matching.
    
    Removes common prefixes/suffixes, short codes, and phone numbers.
    Replaces special characters with spaces.
    """
    if not isinstance(text, str):
        return ""
    
    text = text.lower().strip()
    
    # Remove common prefixes
    prefixes = ["sq *", "tst*", "purchase at ", "payment to ", "transfer to "]
    for p in prefixes:
        if text.startswith(p):
            text = text[len(p):].strip()
            
    # Remove common junk
    # 1. Phone numbers (approximate)
    text = re.sub(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', '', text)
    # 2. Store codes (e.g. #1234)
    text = re.sub(r'#\d+', '', text)
    
    # 3. Replace special chars with space (e.g. *, -, .)
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    
    # 4. Collapse multiple spaces
    text = re.sub(r'\s+', ' ', text)
    
    return text.strip()


def _catalog_match_row_fuzzy(merchant: str, catalog: list[dict], threshold: int = 85) -> dict | None:
    """
    Fuzzy match merchant against catalog aliases using RapidFuzz.
    """
    from rapidfuzz import process, fuzz
    
    norm_merchant = normalize_merchant(merchant)
    if not norm_merchant:
        return None
        
    # Flatten catalog: list of (alias, merchant_dict)
    choices = []
    for m in catalog:
        canonical = m.get("canonical_name", "").lower()
        if canonical:
            choices.append((canonical, m))
        for alias in m.get("aliases", []):
            if alias:
                choices.append((alias.lower(), m))
                
    if not choices:
        return None
        
    # Extract best match
    # choices is list of (string, object)
    # process.extractOne returns (match, score, index) or (match, score, index, choice_key) depending on input
    # We pass dict or list of strings usually, but here we have custom structure.
    # Simpler approach: create map {alias: m}
    
    alias_map = {}
    for alias, m in choices:
        alias_map[alias] = m
        
    aliases = list(alias_map.keys())
    result = process.extractOne(norm_merchant, aliases, scorer=fuzz.token_set_ratio)
    
    if result:
        match_alias, score, _ = result
        if score >= threshold:
            m = alias_map[match_alias]
            return {
                "canonical_name": m.get("canonical_name", ""),
                "category": m.get("category", ""),
                "subcategory": m.get("subcategory", ""),
            }
            
    return None


def apply_catalog_overlay(
    df: pd.DataFrame,
    catalog: list[dict],
    text_column: str = "merchant",
    use_llm_fallback: bool = False,
    llm_provider: str = "groq",
) -> pd.DataFrame:
    """
    Overlay catalog matches using fuzzy logic, with optional LLM fallback.

    Matching order:
      1. Fuzzy match against catalog aliases (RapidFuzz, threshold=85)
      2. LLM standardization for remaining unmatched rows (if use_llm_fallback=True)

    Adds/updates: merchant_standardized, category, subcategory,
                  match_type ('catalog' | 'llm' | 'ml' | 'none').
    """
    if df.empty or not catalog:
        df = df.copy()
        if "merchant_standardized" not in df.columns:
            df["merchant_standardized"] = df.get(text_column, "")
        return df

    df = df.copy()
    if "merchant_standardized" not in df.columns:
        df["merchant_standardized"] = df[text_column].astype(str)
    if "category" not in df.columns:
        df["category"] = pd.NA
    if "subcategory" not in df.columns:
        df["subcategory"] = pd.NA
    if "match_type" not in df.columns:
        df["match_type"] = "ml"

    from rapidfuzz import process, fuzz

    # --- Build alias map once ---
    alias_map: dict[str, dict] = {}
    for m in catalog:
        canonical = m.get("canonical_name", "").lower()
        if canonical:
            alias_map[canonical] = m
        for alias in m.get("aliases", []):
            if alias:
                alias_map[alias.lower()] = m

    aliases = list(alias_map.keys())

    # --- Stage 1: Fuzzy catalog match ---
    for i, merchant in enumerate(df[text_column]):
        norm_merchant = normalize_merchant(str(merchant))
        if not norm_merchant:
            continue

        result = process.extractOne(norm_merchant, aliases, scorer=fuzz.token_set_ratio)
        if result:
            match_alias, score, _ = result
            if score >= 85:
                m = alias_map[match_alias]
                df.at[i, "merchant_standardized"] = m.get("canonical_name", "")
                df.at[i, "category"] = m.get("category", "")
                df.at[i, "subcategory"] = m.get("subcategory", "")
                df.at[i, "match_type"] = "catalog"

    # --- Stage 2: LLM fallback for unmatched rows ---
    if use_llm_fallback:
        try:
            from src.llm_client import standardize_merchant_with_llm
        except ImportError:
            logger.warning("src.llm_client not found; skipping LLM fallback.")
            return df

        # Only process rows that are NOT already catalog-matched or internal
        unmatched_mask = ~df["match_type"].isin(["catalog", "internal"])
        unmatched_indices = df[unmatched_mask].index.tolist()

        if unmatched_indices:
            logger.info(
                "Running LLM fallback for %d unmatched merchants (provider=%s)",
                len(unmatched_indices), llm_provider,
            )

        llm_call_count = 0
        for i in unmatched_indices:
            merchant_raw = str(df.at[i, text_column])
            try:
                llm_name, confidence = standardize_merchant_with_llm(
                    merchant_raw, provider=llm_provider
                )
                df.at[i, "merchant_standardized"] = llm_name
                df.at[i, "match_type"] = "llm"
                llm_call_count += 1
            except Exception as e:
                logger.warning("LLM fallback failed for '%s': %s", merchant_raw, e)

        if llm_call_count:
            logger.info("LLM standardized %d merchants.", llm_call_count)

    return df


def enrich_transactions(
    df: pd.DataFrame,
    models_dir: Path | str | None = None,
    catalog_path: Path | str | None = None,
    skip_ml: bool = False,
    use_llm_fallback: bool = False,
    llm_provider: str = "groq",
) -> pd.DataFrame:
    """
    Full enrichment pipeline:
      1. ML category/subcategory prediction (RandomForest + SentenceTransformer)
      2. Fuzzy catalog overlay (RapidFuzz, threshold=85)
      3. LLM fallback for unmatched merchants (optional, Groq/Gemini/OpenAI)
      4. Internal transaction tagging

    Args:
        df: DataFrame with a 'merchant' column.
        models_dir: Path to trained model artifacts.
        catalog_path: Path to merchant_catalog.json.
        skip_ml: Skip ML prediction (faster, catalog+LLM only).
        use_llm_fallback: Enable LLM for merchants not matched by catalog.
        llm_provider: LLM provider - 'groq' (free), 'gemini', or 'openai'.

    Adds columns: category, subcategory, match_type, merchant_standardized.
    """
    if df.empty:
        return df

    df = df.copy()
    models_dir = Path(models_dir or DEFAULT_MODELS_DIR)
    catalog_path = catalog_path or DEFAULT_CATALOG_PATH

    # 1) ML prediction
    if not skip_ml and models_dir.exists():
        try:
            models = load_classification_models(models_dir)
            df = predict_categories_ml(df, models, text_column="merchant")
        except Exception as e:
            logger.warning("ML classification failed: %s. Proceeding without ML labels.", e)
            df["category"] = df.get("category", pd.NA)
            df["subcategory"] = df.get("subcategory", pd.NA)
            df["match_type"] = "none"
    else:
        if "category" not in df.columns:
            df["category"] = pd.NA
        if "subcategory" not in df.columns:
            df["subcategory"] = pd.NA
        if "match_type" not in df.columns:
            df["match_type"] = "none"

    # 2) Catalog overlay + optional LLM fallback
    catalog = load_merchant_catalog(catalog_path)
    df = apply_catalog_overlay(
        df,
        catalog,
        text_column="merchant",
        use_llm_fallback=use_llm_fallback,
        llm_provider=llm_provider,
    )

    # 3) Internal transaction tagging (always overrides LLM/catalog)
    if "merchant_standardized" not in df.columns:
        df["merchant_standardized"] = df["merchant"].astype(str)
    if "is_internal" in df.columns:
        df.loc[df["is_internal"], "merchant_standardized"] = "Internal Payment"
        df.loc[df["is_internal"], "match_type"] = "internal"

    logger.info(
        "Enriched %d transactions (match_type counts: %s)",
        len(df),
        df["match_type"].value_counts().to_dict(),
    )
    return df
