import json
import hashlib
import logging
import os
import pandas as pd
from datetime import datetime

from google.cloud import bigquery
from dotenv import load_dotenv
from src.parse_statement import parse_statement
from pathlib import Path

# Load environment variables
load_dotenv()

# Configuration
PROJECT_ID = "expensetracker-485100"
DATASET_ID = "credit_card_analytics"
BRONZE_TABLE_ID = "raw_statements"
SILVER_TABLE_ID = "silver_transactions"

# Set credentials if not already in environment
SERVICE_ACCOUNT_KEY = "/Users/kanwarraj/Documents/Expense_Tracker/Keys/expensetracker-485100-0a36841a58b8.json"
if not os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = SERVICE_ACCOUNT_KEY

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_dedup_key(date_val, merchant_raw, amount, user_name):
    """Creates a unique hash key for deduplication: SHA256(date|merchant|amount|user)."""
    raw_string = f"{date_val}|{merchant_raw}|{amount}|{user_name}"
    return hashlib.sha256(raw_string.encode()).hexdigest()


def ensure_silver_table(client):
    """Ensures the silver table exists with correct multi-tenant schema."""
    dataset_id = client.dataset(DATASET_ID)
    table_id = dataset_id.table(SILVER_TABLE_ID)

    schema = [
        bigquery.SchemaField("dedup_key", "STRING"),
        bigquery.SchemaField("user_name", "STRING"),
        bigquery.SchemaField("bank_name", "STRING"),
        bigquery.SchemaField("statement_type", "STRING"),
        bigquery.SchemaField("date", "DATE"),
        bigquery.SchemaField("description", "STRING"),
        bigquery.SchemaField("amount", "FLOAT"),
        bigquery.SchemaField("category", "STRING"),
        bigquery.SchemaField("subcategory", "STRING"),
        bigquery.SchemaField("merchant_standardized", "STRING"),
        bigquery.SchemaField("match_type", "STRING"),
        bigquery.SchemaField("transaction_type", "STRING"),
        bigquery.SchemaField("is_internal", "BOOLEAN"),
        bigquery.SchemaField("file_source", "STRING"),
        bigquery.SchemaField("processed_ts", "TIMESTAMP"),
    ]

    try:
        table = client.get_table(table_id)
        # Check if new columns exist, if not, we must recreate or update
        existing_cols = {field.name for field in table.schema}
        if "user_name" not in existing_cols:
            logger.warning("Table schema is outdated. Deleting and recreating for multi-tenant support.")
            client.delete_table(table_id)
            raise Exception("Trigger recreation")
        logger.info(f"Table {SILVER_TABLE_ID} already exists with correct schema.")
    except Exception:
        logger.info(f"Creating table {SILVER_TABLE_ID}...")
        table = bigquery.Table(table_id, schema=schema)
        client.create_table(table)


def get_existing_dedup_keys(client):
    """Fetches all existing dedup_keys from Silver to prevent duplicates."""
    query = f"SELECT dedup_key FROM `{PROJECT_ID}.{DATASET_ID}.{SILVER_TABLE_ID}`"
    try:
        result = client.query(query).result()
        return {row.dedup_key for row in result}
    except Exception:
        return set()


def load_bronze_to_silver():
    """Fetches raw JSON from Bronze, parses & enriches it, and loads into Silver with Multi-Tenant metadata."""
    client = bigquery.Client(project=PROJECT_ID)
    ensure_silver_table(client)

    # Query Bronze table
    query = f"SELECT file_name, raw_text_content, metadata FROM `{PROJECT_ID}.{DATASET_ID}.{BRONZE_TABLE_ID}`"

    logger.info("Fetching data from Bronze layer...")
    query_job = client.query(query)
    rows = query_job.result()

    all_dataframes = []

    for row in rows:
        file_name = row.file_name
        metadata = row.metadata if row.metadata else {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        bank_id = metadata.get('detected_bank', 'Unknown')
        user_name = metadata.get('owner_name', 'Unknown')
        statement_type = metadata.get('statement_type', 'Credit')

        logger.info(f"Processing {file_name} for user: {user_name}...")

        pdf_path = Path("data/raw") / file_name
        if pdf_path.exists():
            df, detected_bank = parse_statement(
                str(pdf_path),
                bank_id=bank_id,
                enrich=True,
                use_llm_fallback=True,
                llm_provider="groq",
            )
            if not df.empty:
                df['user_name'] = user_name
                df['bank_name'] = detected_bank if detected_bank else bank_id
                df['statement_type'] = statement_type
                df['file_source'] = file_name
                df['processed_ts'] = datetime.utcnow()
                all_dataframes.append(df)
        else:
            logger.warning(f"Original PDF {file_name} not found in data/raw, skipping.")

    if not all_dataframes:
        logger.info("No data available to process.")
        return

    final_df = pd.concat(all_dataframes, ignore_index=True)

    # Column mapping
    final_df = final_df.rename(columns={
        'transaction_date_dt': 'date',
        'merchant': 'description',
    })

    # Ensure 'date' is a date object for BigQuery DATE field
    if 'date' in final_df.columns:
        final_df['date'] = pd.to_datetime(final_df['date']).dt.date

    # Create dedup keys (Now including user_name in the hash!)
    final_df['dedup_key'] = final_df.apply(
        lambda r: create_dedup_key(r.get('date'), r.get('description'), r.get('amount'), r.get('user_name')),
        axis=1,
    )

    # --- DEDUPLICATION ---
    existing_keys = get_existing_dedup_keys(client)
    before_count = len(final_df)
    final_df = final_df[~final_df['dedup_key'].isin(existing_keys)]
    skipped = before_count - len(final_df)
    if skipped > 0:
        logger.info(f"Deduplication: Skipped {skipped} rows that already exist in Silver.")

    if final_df.empty:
        logger.info("No new transactions to load (all duplicates).")
        return

    # Ensure only columns in our Silver schema are sent
    schema_cols = [
        'dedup_key', 'user_name', 'bank_name', 'statement_type', 'date', 'description', 
        'amount', 'category', 'subcategory', 'merchant_standardized', 'match_type', 
        'transaction_type', 'is_internal', 'file_source', 'processed_ts',
    ]
    for col in schema_cols:
        if col not in final_df.columns:
            final_df[col] = None
    final_df = final_df[schema_cols]

    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{SILVER_TABLE_ID}"
    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")

    logger.info(f"Loading {len(final_df)} new transactions into {SILVER_TABLE_ID}...")
    client.load_table_from_dataframe(final_df, table_ref, job_config=job_config).result()
    logger.info("Successfully loaded data to Silver layer.")


if __name__ == "__main__":
    load_bronze_to_silver()
