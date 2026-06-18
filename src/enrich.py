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


# ---------------------------------------------------------------------------
# Regex Heuristic Classifier (Stage 0 — fires before catalog and ML)
# ---------------------------------------------------------------------------

# Each rule: (compiled_pattern, canonical_name, category, subcategory)
# Patterns match against the *raw* merchant description (case-insensitive).
_REGEX_RULES: list[tuple] = [
    # --- Rideshare & Food Delivery ---
    (re.compile(r'\buber\b', re.I),                          "Uber",              "Rideshare & Food Delivery", "Rideshare"),
    (re.compile(r'uber\s*eats', re.I),                       "Uber Eats",         "Rideshare & Food Delivery", "Food Delivery"),
    (re.compile(r'\bdoordash\b', re.I),                      "DoorDash",          "Rideshare & Food Delivery", "Food Delivery"),
    (re.compile(r'\bskip\s*the\s*dishes\b|\bskipthedishes\b', re.I), "SkipTheDishes", "Rideshare & Food Delivery", "Food Delivery"),
    (re.compile(r'\blyft\b', re.I),                          "Lyft",              "Rideshare & Food Delivery", "Rideshare"),
    # --- Telecommunications ---
    (re.compile(r'\brogers\b', re.I),                        "Rogers",            "Telecommunications",        "Mobile Provider"),
    (re.compile(r'\bbell\s*canada|\bbell\s*mobility', re.I), "Bell Canada",       "Telecommunications",        "Mobile Provider"),
    (re.compile(r'\btelus\b', re.I),                         "Telus",             "Telecommunications",        "Mobile Provider"),
    (re.compile(r'\bfido\b', re.I),                          "Fido",              "Telecommunications",        "Mobile Provider"),
    (re.compile(r'\bshaw\b', re.I),                          "Shaw",              "Telecommunications",        "Internet"),
    (re.compile(r'\bkoodo\b', re.I),                         "Koodo",             "Telecommunications",        "Mobile Provider"),
    (re.compile(r'\bvirgin\s*plus|\bvirgin\s*mobile', re.I), "Virgin Plus",       "Telecommunications",        "Mobile Provider"),
    (re.compile(r'\bvideotron\b', re.I),                     "Videotron",         "Telecommunications",        "Internet"),
    # --- Streaming & Entertainment ---
    (re.compile(r'\bnetflix\b', re.I),                       "Netflix",           "Entertainment & Events",    "Streaming"),
    (re.compile(r'\bspotify\b', re.I),                       "Spotify",           "Entertainment & Events",    "Streaming"),
    (re.compile(r'\bdisney\s*\+|\bdisney\s*plus', re.I),     "Disney+",           "Entertainment & Events",    "Streaming"),
    (re.compile(r'\bcrave\b', re.I),                         "Crave",             "Entertainment & Events",    "Streaming"),
    (re.compile(r'youtube\s*premium|youtubepremium', re.I),  "YouTube Premium",   "Entertainment & Events",    "Streaming"),
    (re.compile(r'\bapple\s*tv\+', re.I),                    "Apple TV+",         "Entertainment & Events",    "Streaming"),
    (re.compile(r'\bparamount\s*\+', re.I),                  "Paramount+",        "Entertainment & Events",    "Streaming"),
    # --- Software & Services ---
    (re.compile(r'\bopenai|\bchatgpt', re.I),                "OpenAI",            "Software & Services",       "AI/API Services"),
    (re.compile(r'\bgithub\b', re.I),                        "GitHub",            "Software & Services",       "Developer Tools"),
    (re.compile(r'\bheroku\b', re.I),                        "Heroku",            "Software & Services",       "Cloud Services"),
    (re.compile(r'\baws\b|amazon\s*web\s*services', re.I),   "AWS",               "Software & Services",       "Cloud Services"),
    (re.compile(r'\bgoogle\s*cloud|gcp\b', re.I),            "Google Cloud",      "Software & Services",       "Cloud Services"),
    (re.compile(r'google\s*\*?one\b|google\s*storage', re.I),"Google One",        "Software & Services",       "Cloud Storage"),
    (re.compile(r'\bmicrosoft|\bmsft\b|\boffice\s*365', re.I),"Microsoft",         "Software & Services",       "Productivity"),
    (re.compile(r'\badobe\b', re.I),                         "Adobe",             "Software & Services",       "Creative Software"),
    (re.compile(r'\bslack\b', re.I),                         "Slack",             "Software & Services",       "Productivity"),
    (re.compile(r'\bnotion\b', re.I),                        "Notion",            "Software & Services",       "Productivity"),
    # --- Food & Dining ---
    (re.compile(r'\btim\s*hortons|\btims\b', re.I),          "Tim Hortons",       "Food & Dining",             "Coffee & Fast Food"),
    (re.compile(r'\bstarbucks\b', re.I),                     "Starbucks",         "Food & Dining",             "Coffee"),
    (re.compile(r'\bmcdonalds|\bmc\s*donald', re.I),         "McDonald's",        "Food & Dining",             "Fast Food"),
    (re.compile(r'\bsubway\b', re.I),                        "Subway",            "Food & Dining",             "Fast Food"),
    (re.compile(r'\bchipotle\b', re.I),                      "Chipotle",          "Food & Dining",             "Fast Food"),
    # --- Groceries ---
    (re.compile(r'\bloblaws\b|\bpc\s*express', re.I),        "Loblaws",           "Groceries",                 "Supermarket"),
    (re.compile(r'\bsobeys\b', re.I),                        "Sobeys",            "Groceries",                 "Supermarket"),
    (re.compile(r'\bfreshco\b', re.I),                       "FreshCo",           "Groceries",                 "Supermarket"),
    (re.compile(r'\bmetro\s*inc|\bmetro\s*grocery', re.I),   "Metro",             "Groceries",                 "Supermarket"),
    (re.compile(r'\bcostco\b', re.I),                        "Costco",            "Groceries",                 "Warehouse Club"),
    (re.compile(r'\bwhole\s*foods', re.I),                   "Whole Foods",       "Groceries",                 "Supermarket"),
    (re.compile(r'\bno\s*frills\b', re.I),                   "No Frills",         "Groceries",                 "Discount Grocery"),
    (re.compile(r'\bfood\s*basics', re.I),                   "Food Basics",       "Groceries",                 "Discount Grocery"),
    # --- Retail ---
    (re.compile(r'\bwalmrt|\bwal\s*mart|\bwalmart', re.I),   "Walmart",           "Retail",                    "General Merchandise"),
    (re.compile(r'\bbest\s*buy', re.I),                      "Best Buy",          "Retail",                    "Electronics"),
    (re.compile(r'\bcanadian\s*tire', re.I),                 "Canadian Tire",     "Retail",                    "Auto & Home"),
    (re.compile(r'\bhome\s*depot', re.I),                    "Home Depot",        "Retail",                    "Home Improvement"),
    (re.compile(r'\bikea\b', re.I),                          "IKEA",              "Retail",                    "Furniture"),
    (re.compile(r'\bthe\s*bay|\bhudsons\s*bay', re.I),       "Hudson's Bay",      "Retail",                    "Department Store"),
    # --- Online Retail ---
    (re.compile(r'\bamzn|\bamazon', re.I),                   "Amazon",            "Online Retail",             "Marketplace"),
    (re.compile(r'\bebay\b', re.I),                          "eBay",              "Online Retail",             "Marketplace"),
    (re.compile(r'\betsy\b', re.I),                          "Etsy",              "Online Retail",             "Marketplace"),
    # --- Transportation ---
    (re.compile(r'\bpetro.canada', re.I),                    "Petro-Canada",      "Transportation",            "Gas Station"),
    (re.compile(r'\bshell\b', re.I),                         "Shell",             "Transportation",            "Gas Station"),
    (re.compile(r'\bimpark\b', re.I),                        "Impark",            "Transportation",            "Parking"),
    (re.compile(r'\bpresto\b', re.I),                        "PRESTO",            "Transportation",            "Transit Card"),
    (re.compile(r'\bttc\b|\btoronto\s*transit', re.I),       "TTC",               "Transportation",            "Public Transit"),
    # --- Health & Pharmacy ---
    (re.compile(r'\bshoppers\s*drug', re.I),                 "Shoppers Drug Mart", "Health & Pharmacy",        "Pharmacy"),
    (re.compile(r'\blon\s*drug|\bpharmaplus', re.I),         "Shoppers Drug Mart", "Health & Pharmacy",        "Pharmacy"),
    (re.compile(r'\brexall\b', re.I),                        "Rexall",            "Health & Pharmacy",         "Pharmacy"),
    # --- Professional Development ---
    (re.compile(r'\blinkedin\b', re.I),                      "LinkedIn",          "Professional Development",  "Online Learning & Career"),
    (re.compile(r'\bdatacamp\b', re.I),                      "DataCamp",          "Professional Development",  "Online Courses"),
    (re.compile(r'\bcoursera\b', re.I),                      "Coursera",          "Professional Development",  "Online Courses"),
    (re.compile(r'\budemy\b', re.I),                         "Udemy",             "Professional Development",  "Online Courses"),
    # --- Internal Payments ---
    (re.compile(r'payment.*thank\s*you|paiement.*merci|credit\s*card\s*payment', re.I), 
                                                              "Internal Payment",  "Internal Payment",          "Credit Card Payment"),
    (re.compile(r'\binterac\s*e.transfer', re.I),            "Interac e-Transfer", "Internal Payment",         "Transfer"),
]


def regex_classify_merchant(text: str) -> dict | None:
    """
    Run deterministic regex rules against a raw merchant description.

    Returns a dict with canonical_name, category, subcategory if matched,
    or None if no rule applies.
    """
    if not text:
        return None
    for pattern, canonical, category, subcategory in _REGEX_RULES:
        if pattern.search(text):
            return {
                "canonical_name": canonical,
                "category": category,
                "subcategory": subcategory,
            }
    return None


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
      0. Regex heuristics (deterministic, instant — covers top 50 Canadian merchants)
      1. Fuzzy match against catalog aliases (RapidFuzz, threshold=85)
      2. LLM joint classification for remaining unmatched rows (if use_llm_fallback=True)

    Adds/updates: merchant_standardized, category, subcategory,
                  match_type ('regex' | 'catalog' | 'llm' | 'ml' | 'none').
    """
    if df.empty:
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

    # --- Stage 0: Regex heuristics (fast, deterministic) ---
    for i, merchant in enumerate(df[text_column]):
        match = regex_classify_merchant(str(merchant))
        if match:
            df.at[i, "merchant_standardized"] = match["canonical_name"]
            df.at[i, "category"] = match["category"]
            df.at[i, "subcategory"] = match["subcategory"]
            df.at[i, "match_type"] = "regex"

    # --- Build alias map once for catalog matching ---
    alias_map: dict[str, dict] = {}
    for m in catalog:
        canonical = m.get("canonical_name", "").lower()
        if canonical:
            alias_map[canonical] = m
        for alias in m.get("aliases", []):
            if alias:
                alias_map[alias.lower()] = m

    aliases = list(alias_map.keys())

    # --- Stage 1: Fuzzy catalog match (skips already-matched rows) ---
    for i, merchant in enumerate(df[text_column]):
        # Skip rows already classified by regex
        if df.at[i, "match_type"] == "regex":
            continue

        norm_merchant = normalize_merchant(str(merchant))
        if not norm_merchant or not aliases:
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

        # Only process rows that are NOT already regex/catalog-matched or internal
        unmatched_mask = ~df["match_type"].isin(["regex", "catalog", "internal"])
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
                llm_name, llm_category, llm_subcategory, _ = standardize_merchant_with_llm(
                    merchant_raw, provider=llm_provider
                )
                df.at[i, "merchant_standardized"] = llm_name
                df.at[i, "category"] = llm_category
                df.at[i, "subcategory"] = llm_subcategory
                df.at[i, "match_type"] = "llm"
                llm_call_count += 1
            except Exception as e:
                logger.warning("LLM fallback failed for '%s': %s", merchant_raw, e)

        if llm_call_count:
            logger.info("LLM classified %d merchants (name + category + subcategory).", llm_call_count)

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
