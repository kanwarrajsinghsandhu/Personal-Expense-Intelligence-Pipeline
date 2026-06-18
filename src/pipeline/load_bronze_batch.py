"""
Batch Bronze Layer Ingestion: Ingest all PDFs in the data/raw folder.
"""

import os
import logging
from pathlib import Path
from src.pipeline.load_bronze_sim import ingest_file_to_bronze

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
RAW_DATA_DIR = Path("data/raw")
DEFAULT_USER = "Kanwar"
DEFAULT_TYPE = "Credit"

def run_batch_ingestion():
    """Finds all PDFs in RAW_DATA_DIR and processes them."""
    if not RAW_DATA_DIR.exists():
        logger.error(f"Directory {RAW_DATA_DIR} does not exist.")
        return

    pdf_files = [f for f in RAW_DATA_DIR.glob("*.pdf") if "Sample-RBC" not in f.name]
    logger.info(f"Found {len(pdf_files)} Scotia PDF files in {RAW_DATA_DIR} (excluded RBC)")

    success_count = 0
    failure_count = 0

    for pdf_path in pdf_files:
        try:
            logger.info(f"--- Processing {pdf_path.name} ---")
            ingest_file_to_bronze(
                file_path=str(pdf_path),
                user_name=DEFAULT_USER,
                statement_type=DEFAULT_TYPE
            )
            success_count += 1
        except Exception as e:
            logger.error(f"Failed to process {pdf_path.name}: {e}")
            failure_count += 1

    logger.info("=" * 40)
    logger.info("Batch Ingestion Complete")
    logger.info(f"Successfully processed: {success_count}")
    logger.info(f"Failed: {failure_count}")
    logger.info("=" * 40)

if __name__ == "__main__":
    run_batch_ingestion()
