"""
Build Phase: Train Category and Subcategory classifiers for Expense Tracker.

This script loads labeled transaction data, converts raw merchant text into
semantic embeddings (the "Translator"), trains RandomForest models (the "Brain"),
evaluates on Canadian bank-style data, and serializes everything for inference.

Run from project root: python -m src.1_train_model
"""

from __future__ import annotations

import logging
from pathlib import Path

import joblib
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
from sentence_transformers import SentenceTransformer

# Project paths - pathlib ensures cross-platform compatibility
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Support both data/training and data/trainings (common typo)
TRAINING_CSV = PROJECT_ROOT / "data" / "training" / "FinalTransaction.csv"
if not TRAINING_CSV.exists():
    TRAINING_CSV = PROJECT_ROOT / "data" / "trainings" / "FinalTransaction.csv"
MODELS_DIR = PROJECT_ROOT / "models"

logger = logging.getLogger(__name__)


def main() -> None:
    """Execute the full training pipeline."""
    logger.info("=" * 60)
    logger.info("Expense Tracker - Build Phase: Training Classifiers")
    logger.info("=" * 60)

    # 1. Initialize Sentence Transformer (semantic embeddings)
    logger.info("Loading SentenceTransformer model (all-MiniLM-L6-v2)...")
    embedder = SentenceTransformer("all-MiniLM-L6-v2")

    # 2. Load and validate data
    df = load_and_prepare_data()
    if df is None:
        return

    # 3. Vectorization - convert raw_text to 384-dim embeddings
    X = vectorize_text(df["raw_text"], embedder)

    # 4. Encode targets and split data
    y_category, y_subcategory, encoders = encode_targets(df)
    X_train, X_test, y_cat_train, y_cat_test, y_sub_train, y_sub_test = split_data(
        X, y_category, y_subcategory
    )

    # 5. Train both classifiers
    clf_category = train_category_model(X_train, y_cat_train)
    clf_subcategory = train_subcategory_model(X_train, y_sub_train)

    # 6. Evaluation - focus on Category model (primary metric)
    evaluate_models(clf_category, clf_subcategory, X_test, y_cat_test, y_sub_test, encoders)

    # 7. Serialize all artifacts
    save_artifacts(clf_category, clf_subcategory, encoders)

    logger.info("Build phase complete. Models ready for inference.")


def load_and_prepare_data() -> pd.DataFrame | None:
    """
    Load CSV and drop rows with missing raw_text or category.

    Why: Garbage in, garbage out. Missing labels or empty text would corrupt
    training and cause runtime errors during inference.
    """
    try:
        df = pd.read_csv(TRAINING_CSV, encoding="utf-8")
    except FileNotFoundError:
        logger.error("Training file not found: %s", TRAINING_CSV)
        return None
    except Exception as e:
        logger.error("Failed to load CSV: %s", e)
        return None

    initial_rows = len(df)
    df = df.dropna(subset=["raw_text", "category"])
    df = df.dropna(subset=["subcategory"])
    dropped = initial_rows - len(df)
    if dropped > 0:
        logger.warning("Dropped %d rows with missing raw_text, category, or subcategory", dropped)

    logger.info("Dataset shape: %s", df.shape)
    logger.info("Class distribution (Category):")
    for cat, count in df["category"].value_counts().items():
        logger.info("  %s: %d", cat, count)

    return df


def vectorize_text(raw_text: pd.Series, embedder: SentenceTransformer) -> "np.ndarray":
    """
    Convert raw merchant text into 384-dimensional semantic embeddings.

    Why: SentenceTransformer captures meaning (e.g., "Tim Hortons" and "Starbucks"
    are both coffee shops) better than bag-of-words. Essential for messy Canadian
    bank statement formats.
    """
    logger.info("Vectorizing raw_text (output shape: n_samples x 384)...")
    embeddings = embedder.encode(raw_text.tolist(), show_progress_bar=True)
    return embeddings


def encode_targets(
    df: pd.DataFrame,
) -> tuple[list[int], list[int], dict[str, LabelEncoder]]:
    """
    Encode category and subcategory labels to integers.

    Why: RandomForest works with integers. LabelEncoder + saved pickle allows
    inverse_transform during inference to map predictions back to human labels.
    """
    enc_cat = LabelEncoder()
    enc_sub = LabelEncoder()
    y_category = enc_cat.fit_transform(df["category"])
    y_subcategory = enc_sub.fit_transform(df["subcategory"])
    encoders = {"category": enc_cat, "subcategory": enc_sub}
    logger.info("Encoded %d categories, %d subcategories", len(enc_cat.classes_), len(enc_sub.classes_))
    return y_category, y_subcategory, encoders


def split_data(
    X: "np.ndarray",
    y_category: list[int],
    y_subcategory: list[int],
    test_size: float = 0.2,
    random_state: int = 42,
) -> tuple:
    """80/20 train/test split with consistent stratification on category."""
    X_train, X_test, y_cat_train, y_cat_test, y_sub_train, y_sub_test = train_test_split(
        X, y_category, y_subcategory, test_size=test_size, random_state=random_state, stratify=y_category
    )
    logger.info("Train size: %d, Test size: %d", len(X_train), len(X_test))
    return X_train, X_test, y_cat_train, y_cat_test, y_sub_train, y_sub_test


def train_category_model(X_train: "np.ndarray", y_train: list[int]) -> RandomForestClassifier:
    """Train the primary Category classifier."""
    logger.info("Training Category classifier...")
    clf = RandomForestClassifier(n_estimators=100, random_state=42, n_jobs=-1)
    clf.fit(X_train, y_train)
    return clf


def train_subcategory_model(X_train: "np.ndarray", y_train: list[int]) -> RandomForestClassifier:
    """Train the Subcategory classifier (secondary taxonomy)."""
    logger.info("Training Subcategory classifier...")
    clf = RandomForestClassifier(n_estimators=100, random_state=42, n_jobs=-1)
    clf.fit(X_train, y_train)
    return clf


def evaluate_models(
    clf_category: RandomForestClassifier,
    clf_subcategory: RandomForestClassifier,
    X_test: "np.ndarray",
    y_cat_test: list[int],
    y_sub_test: list[int],
    encoders: dict[str, LabelEncoder],
) -> None:
    """
    Print classification reports to assess performance on Canadian bank data.

    Why: Precision/Recall reveal how well the model handles messy inputs
    (abbreviated merchant names, different formats). Crucial for deployment confidence.
    """
    logger.info("=" * 60)
    logger.info("Category Model - Classification Report")
    logger.info("=" * 60)
    y_cat_pred = clf_category.predict(X_test)
    target_names = encoders["category"].classes_
    print(
        classification_report(
            y_cat_test, y_cat_pred, target_names=target_names, zero_division=0
        )
    )

    logger.info("Subcategory Model - Classification Report")
    logger.info("=" * 60)
    y_sub_pred = clf_subcategory.predict(X_test)
    target_names_sub = encoders["subcategory"].classes_
    # Use labels= to handle test set not containing all 130 subcategories
    print(
        classification_report(
            y_sub_test,
            y_sub_pred,
            labels=range(len(target_names_sub)),
            target_names=target_names_sub,
            zero_division=0,
        )
    )


def save_artifacts(
    clf_category: RandomForestClassifier,
    clf_subcategory: RandomForestClassifier,
    encoders: dict[str, LabelEncoder],
) -> None:
    """
    Serialize models and encoders to models/ directory.

    Why: joblib preserves sklearn objects and numpy arrays efficiently.
    Inference engine will load these to map embeddings -> labels.
    """
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    artifacts = [
        (MODELS_DIR / "category_classifier.pkl", clf_category),
        (MODELS_DIR / "subcategory_classifier.pkl", clf_subcategory),
        (MODELS_DIR / "category_encoder.pkl", encoders["category"]),
        (MODELS_DIR / "subcategory_encoder.pkl", encoders["subcategory"]),
    ]

    for path, obj in artifacts:
        try:
            joblib.dump(obj, path)
            logger.info("Saved: %s", path)
        except OSError as e:
            logger.error("Failed to save %s: %s", path, e)
            raise


if __name__ == "__main__":
    main()
