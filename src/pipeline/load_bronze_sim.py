"""
Bronze Layer Ingestion: Load raw PDF content into BigQuery.

Refactored to support multi-tenant dynamic inputs:
--file: Path to the PDF
--user: Name of the person the statement belongs to
--type: Credit or Debit
"""

import json
import logging
import os
import argparse
from datetime import datetime
from pathlib import Path
from google.cloud import bigquery
from dotenv import load_dotenv
from src.parsing.extract_pdf import extract_pdf

# Load environment variables (GOOGLE_APPLICATION_CREDENTIALS)
load_dotenv()

# Configuration
PROJECT_ID = "expensetracker-485100"
DATASET_ID = "credit_card_analytics"
TABLE_ID = "raw_statements"

# Set credentials if not already in environment
SERVICE_ACCOUNT_KEY = "Keys/expensetracker-485100-0a36841a58b8.json"
if not os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = SERVICE_ACCOUNT_KEY

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def create_bronze_table_schema():
    """Defines the schema for the raw ingestion table."""
    return [
        bigquery.SchemaField("ingestion_ts", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("file_name", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("file_source", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("raw_text_content", "JSON", mode="REQUIRED"),
        bigquery.SchemaField("metadata", "JSON", mode="NULLABLE"),
    ]

def ensure_dataset_and_table(client):
    """Checks if dataset and table exist, creates them if not."""
    dataset_ref = client.dataset(DATASET_ID)
    try:
        client.get_dataset(dataset_ref)
        logger.info(f"Dataset {DATASET_ID} already exists.")
    except Exception:
        logger.info(f"Creating dataset {DATASET_ID}...")
        client.create_dataset(bigquery.Dataset(dataset_ref))

    table_ref = dataset_ref.table(TABLE_ID)
    try:
        client.get_table(table_ref)
        logger.info(f"Table {TABLE_ID} already exists.")
    except Exception:
        logger.info(f"Creating table {TABLE_ID}...")
        schema = create_bronze_table_schema()
        table = bigquery.Table(table_ref, schema=schema)
        client.create_table(table)

def ingest_file_to_bronze(file_path: str, user_name: str = "Unknown", statement_type: str = "Credit"):
    """
    Reads a PDF, extracts raw text, and prepares it for BigQuery insertion.
    Captures multi-tenant metadata (user, type).
    """
    path = Path(file_path)
    if not path.exists():
        logger.error(f"File not found: {file_path}")
        return

    logger.info(f"Extracting raw content from {path.name} for user: {user_name}...")
    
    # 1. Extract raw text (using existing logic)
    pages_text, _, detected_bank = extract_pdf(path)

    # 2. Construct the Bronze Record
    record = {
        "ingestion_ts": datetime.utcnow().isoformat(),
        "file_name": path.name,
        "file_source": "local_upload",
        "raw_text_content": json.dumps(pages_text),
        "metadata": json.dumps({
            "detected_bank": detected_bank,
            "page_count": len(pages_text),
            "owner_name": user_name,
            "statement_type": statement_type,
            "upload_month": datetime.now().strftime("%Y-%m")
        })
    }

    # 3. Load to BigQuery
    client = bigquery.Client(project=PROJECT_ID)
    
    # Ensure infrastructure exists
    ensure_dataset_and_table(client)
    
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"
    logger.info(f"Inserting record into {table_ref}...")
    errors = client.insert_rows_json(table_ref, [record])
    
    if errors:
        logger.error(f"Encountered errors while inserting rows: {errors}")
    else:
        logger.info(f"Successfully loaded data to Bronze layer for {user_name}.")
    
    return record

def main():
    parser = argparse.ArgumentParser(description="Ingest bank statement PDF to BigQuery Bronze layer.")
    parser.add_argument("--file", required=True, help="Path to the PDF file")
    parser.add_argument("--user", default="Kanwar", help="Name of the person the statement belongs to")
    parser.add_argument("--type", choices=["Credit", "Debit"], default="Credit", help="Type of statement (Credit/Debit)")
    
    args = parser.parse_args()
    
    ingest_file_to_bronze(
        file_path=args.file,
        user_name=args.user,
        statement_type=args.type
    )

if __name__ == "__main__":
    main()
