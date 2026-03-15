"""
Expense Tracker - AI-powered transaction classification for the Canadian market.

A Hybrid AI approach combining Semantic Embeddings (SentenceTransformers) with
Machine Learning (RandomForest) to classify credit card transactions.
"""

__version__ = "0.1.0"

import logging
import sys

# Configure logging for all scripts in the package
# INFO level with timestamp ensures readable output during training/inference
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
