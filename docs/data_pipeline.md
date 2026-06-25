# Data Pipeline & ETL

This document describes the end-to-end data flow from bank statement PDF to BigQuery Gold layer.

---

## Pipeline Overview

```
PDFs (data/raw/)
    ↓
[1. Bronze Ingestion] → raw_statements table (BigQuery)
    ↓
[2. Silver Enrichment] → silver_transactions table (BigQuery)
    ↓
[3. Gold Layer (dbt)] → 6 analytics marts (BigQuery)
    ↓
[4. Forecasting] → spend_forecast table (BigQuery)
    ↓
[Streamlit Dashboard] ← queries Gold + Forecast tables
```

---

## Stage 1: Bronze Ingestion

### Purpose

Ingest raw PDF text into BigQuery as an immutable audit trail. No transformation happens here — just extract and store.

### Input

Bank statement PDFs in `data/raw/`:
- **RBC:** Credit card e-statements (Jan 2024 – Dec 2025)
- **Scotiabank:** Debit account e-statements (Jan 2025 – May 2026)

### Process

**File:** `src/pipeline/load_bronze_sim.py` (single PDF) or `load_bronze_batch.py` (all PDFs)

**Steps:**

1. **PDF Parsing:** Use `pdfplumber` to extract raw text from each page
2. **Bank Detection:** Identify bank from page content (RBC logo, Scotiabank header, etc.)
3. **Metadata Extraction:** Detect statement type (Credit/Debit), owner name, month/year
4. **JSON Serialization:** Store raw page text as JSON strings
5. **BigQuery Insert:** Append one row per PDF to `raw_statements` table

### BigQuery Schema

```sql
CREATE TABLE credit_card_analytics.raw_statements (
  ingestion_ts TIMESTAMP,          -- UTC timestamp of processing
  file_name STRING,                -- original PDF filename
  file_source STRING,              -- e.g., 'data/raw/RBC_Jan2024.pdf'
  raw_text_content JSON,           -- extracted text from all pages
  metadata JSON,                   -- {detected_bank, page_count, owner_name, statement_type, upload_month}
  file_hash STRING,                -- SHA-256 hash for integrity check
);
```

### Idempotency

**Problem:** If the same PDF is ingested twice, duplicate rows appear in Bronze.

**Mitigation:** Use `file_hash` (SHA-256 of PDF content) to detect re-ingestion. Before insert, check if `file_hash` already exists in `raw_statements`. If yes, skip.

### Command Line Usage

```bash
# Single PDF
python src/pipeline/load_bronze_sim.py \
  data/raw/RBC_Statement_Jan2024.pdf \
  Kanwar \
  Credit

# All PDFs in data/raw/
python src/pipeline/load_bronze_batch.py
```

---

## Stage 2: Silver Enrichment

### Purpose

Transform Bronze raw text into clean, deduplicated, enriched transactions ready for analytics.

### Process

**File:** `src/pipeline/load_silver.py`

**Steps:**

1. **Fetch Bronze Rows:** Query all rows from `raw_statements`
2. **Re-Parse:** For each Bronze row, re-read the original PDF file from `data/raw/` and call `parse_statement()`
3. **Enrichment:** Call enrichment cascade (see [ML & Forecasting](ml_forecasting.md) for details)
   - Regex heuristics
   - Fuzzy catalog lookup
   - ML classifier
   - LLM fallback (with SQLite cache)
4. **Deduplication:** Check new transactions against existing Silver `dedup_key` values; drop duplicates
5. **BigQuery Insert:** Append deduplicated transactions to `silver_transactions`

### BigQuery Schema

```sql
CREATE TABLE credit_card_analytics.silver_transactions (
  -- Surrogate key
  dedup_key STRING,                -- SHA-256(date | merchant | amount | user)
  
  -- Tenant & Source
  user_name STRING,                -- multi-tenant identifier
  bank_name STRING,                -- e.g., 'RBC', 'Scotiabank'
  statement_type STRING,           -- 'Credit' or 'Debit'
  statement_id STRING,             -- unique statement identifier
  statement_year_month STRING,     -- e.g., '2024-01'
  
  -- Core transaction fields
  date DATE,
  description STRING,              -- raw merchant description from statement
  amount FLOAT64,                  -- transaction amount (negative for credits)
  
  -- Enrichment results
  category STRING,                 -- e.g., 'Food & Dining'
  subcategory STRING,              -- e.g., 'Coffee Shops'
  merchant_standardized STRING,    -- canonical merchant name
  match_type STRING,               -- REGEX_RULE, CATALOG, ML_CLASSIFIER, LLM_FALLBACK
  transaction_type STRING,         -- DEBIT, CREDIT, TRANSFER
  is_internal BOOLEAN,             -- TRUE if internal transfer / payment
  
  -- ML Features
  posting_lag_days INT64,          -- days between statement period and posting
  transaction_cycle_day INT64,     -- day of month transaction posted
  transaction_weekday STRING,      -- Monday, Tuesday, ..., Sunday
  is_weekend BOOLEAN,              -- TRUE if Saturday or Sunday
  is_foreign_currency BOOLEAN,     -- TRUE if FX transaction
  fx_amount_usd FLOAT64 NULL,      -- converted to USD (if applicable)
  
  -- Metadata
  ingestion_ts TIMESTAMP,          -- when record was loaded into Silver
  load_sequence INT64,             -- sequence number of this load batch
);
```

### Deduplication Logic

Before inserting into Silver, deduplicate based on `dedup_key`:

```python
# Fetch existing dedup keys
existing_keys = bq_client.query(
    "SELECT DISTINCT dedup_key FROM silver_transactions WHERE user_name = @user"
).to_pandas()

# Filter new rows
new_rows = new_rows[~new_rows['dedup_key'].isin(existing_keys)]

# Insert
bq_client.load_table_from_dataframe(new_rows, table_id).result()
```

**Why not a unique constraint?** Bank statements often overlap (last few transactions of month N appear in statement for month N+1). A natural PK would incorrectly reject legitimate repeat transactions from the same merchant on the same day.

### Enrichment Waterfall

See [ML & Forecasting — Transaction Categorization](ml_forecasting.md#part-a-transaction-categorization-4-stage-cascade) for detailed information on the enrichment pipeline.

**Summary:**
- Regex heuristics (~30% match)
- Fuzzy catalog (~50% cumulative)
- ML classifier (~15% cumulative)
- LLM fallback (~5% cumulative)
- **Total: ~100% coverage**

### Command Line Usage

```bash
python src/pipeline/load_silver.py
```

**Typical runtime:** 5–15 minutes (depends on PDF parsing and LLM cache hit rate)

---

## Stage 3: Gold Layer (dbt Analytics Marts)

### Purpose

Create purpose-built analytical tables optimized for dashboard queries. All business logic lives here.

### Technology

**dbt-bigquery:** Write SQL models, leverage templating, generate lineage and documentation.

### Models

**Project:** `expense_tracker_dbt/`

#### Staging Layer

**File:** `models/staging/stg_transactions.sql`

A cleaned view over `silver_transactions`:
- Renames columns for clarity
- Coalesces `merchant_standardized` with raw description
- Adds computed columns (e.g., `is_external = NOT is_internal`)
- Filters out internal transfers

**Materialization:** `view` (no storage cost, recomputed on every query)

#### Mart Layer (6 Tables)

All materialized as `table` (BigQuery native tables) for performance.

##### 1. monthly_category_spend

**Purpose:** Aggregate spend by category over time (input for forecasting)

**Key Columns:**
```sql
user_name STRING,
bank_name STRING,
statement_type STRING,
month DATE,                  -- first day of month (2024-01-01 format)
category STRING,
total_spend FLOAT64,         -- SUM(amount)
transaction_count INT64,     -- COUNT(*)
avg_transaction FLOAT64,     -- AVG(amount)
```

**Query Pattern:**
```sql
SELECT
  user_name, bank_name, statement_type, 
  DATE_TRUNC(date, MONTH) as month,
  category,
  SUM(CAST(amount AS FLOAT64)) as total_spend,
  COUNT(*) as transaction_count,
  AVG(CAST(amount AS FLOAT64)) as avg_transaction
FROM {{ ref('stg_transactions') }}
WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 36 MONTH)
  AND is_external
GROUP BY 1, 2, 3, 4, 5
```

**Refresh:** Full table materialization (WRITE_TRUNCATE) on each dbt run

##### 2. spending_trends

**Purpose:** Month-over-month change in category spending

**Key Columns:**
```sql
user_name STRING,
category STRING,
month DATE,
total_spend FLOAT64,
prev_month_spend FLOAT64,
mom_change FLOAT64,          -- total_spend - prev_month_spend
mom_change_pct FLOAT64,      -- (total_spend - prev_month_spend) / prev_month_spend
```

**Query Pattern:**
```sql
SELECT
  user_name, category, month, total_spend,
  LAG(total_spend) OVER (PARTITION BY user_name, category ORDER BY month) as prev_month_spend,
  total_spend - LAG(total_spend) OVER (...) as mom_change,
  ...
FROM {{ ref('monthly_category_spend') }}
```

**Use Case:** Dashboard charts for "spending is up/down X% this month"

##### 3. spending_anomalies

**Purpose:** Flag unusual transactions as anomalies

**Key Columns:**
```sql
user_name STRING,
category STRING,
date DATE,
merchant_standardized STRING,
amount FLOAT64,
category_mean FLOAT64,       -- avg spend in this category
category_stddev FLOAT64,     -- std dev in this category
z_score FLOAT64,             -- (amount - mean) / stddev
anomaly_level STRING,        -- 'Normal', 'Moderate', 'High'
```

**Logic:**
```sql
WITH category_stats AS (
  SELECT
    category,
    AVG(amount) as cat_mean,
    STDDEV_POP(amount) as cat_stddev
  FROM {{ ref('stg_transactions') }}
  GROUP BY category
)
SELECT
  ...,
  (amount - cat_mean) / NULLIF(cat_stddev, 0) as z_score,
  CASE
    WHEN ABS((amount - cat_mean) / NULLIF(cat_stddev, 0)) > 2 THEN 'High'
    WHEN ABS((amount - cat_mean) / NULLIF(cat_stddev, 0)) > 1 THEN 'Moderate'
    ELSE 'Normal'
  END as anomaly_level
FROM {{ ref('stg_transactions') }} t
LEFT JOIN category_stats s ON t.category = s.category
```

**Thresholds:**
- **Normal:** Z-score between -1 and +1 (68% of data)
- **Moderate:** Z-score between -2 and +2 (95% of data)
- **High:** Z-score > ±2 (outliers, <5% of data)

**Dashboard Use:** "Anomalies" table shows top 20 High Anomaly transactions

##### 4. recurring_expenses

**Purpose:** Identify subscription and habit spending patterns

**Key Columns:**
```sql
user_name STRING,
merchant_standardized STRING,
category STRING,
frequency_name STRING,       -- 'Monthly Subscription', 'Weekly Habit', 'Quarterly'
avg_days_between FLOAT64,
annual_projection FLOAT64,   -- avg_amount * times_per_year
first_seen DATE,
last_seen DATE,
occurrence_count INT64,
```

**Detection Logic:**
```sql
WITH transaction_gaps AS (
  SELECT
    user_name, merchant_standardized, category,
    DATE_DIFF(date, LAG(date) OVER (...), DAY) as days_since_last
  FROM {{ ref('stg_transactions') }}
)
SELECT
  ...,
  CASE
    WHEN days_since_last BETWEEN 25 AND 35 THEN 'Monthly Subscription'
    WHEN days_since_last BETWEEN 6 AND 8 THEN 'Weekly Habit'
    WHEN days_since_last BETWEEN 80 AND 100 THEN 'Quarterly'
    ELSE NULL
  END as frequency_name
FROM transaction_gaps
WHERE frequency_name IS NOT NULL
```

**Dashboard Use:** "Recurring Expenses" table for forecasting fixed commitments

##### 5. merchant_insights

**Purpose:** Per-merchant spending aggregates

**Key Columns:**
```sql
user_name STRING,
merchant_standardized STRING,
category STRING,
subcategory STRING,
total_spent FLOAT64,         -- SUM(amount)
visit_count INT64,           -- COUNT(*)
pct_of_wallet FLOAT64,       -- total_spent / SUM(all spend)
avg_transaction FLOAT64,
first_visit DATE,
last_visit DATE,
```

**Query Pattern:**
```sql
SELECT
  user_name, merchant_standardized, category, subcategory,
  SUM(amount) as total_spent,
  COUNT(*) as visit_count,
  100.0 * SUM(amount) / SUM(SUM(amount)) OVER (PARTITION BY user_name) as pct_of_wallet,
  ...
FROM {{ ref('stg_transactions') }}
WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 12 MONTH)
GROUP BY 1, 2, 3, 4
ORDER BY total_spent DESC
```

**Dashboard Use:** "Top Merchants" chart

##### 6. fixed_vs_variable_spend

**Purpose:** Budget planning — split spend by fixed vs discretionary

**Key Columns:**
```sql
user_name STRING,
month DATE,
category STRING,
spend_type STRING,           -- 'Fixed', 'Variable', 'Transfer'
total_amount FLOAT64,
transaction_count INT64,
```

**Classification:**

- **Fixed/Essential:** Insurance, Utilities, Rent, Subscriptions, Communications, Healthcare
- **Variable/Discretionary:** Dining, Shopping, Entertainment, Gas, Groceries (user-configurable)
- **Transfers:** Internal payments, bill payments

**Query Pattern:**
```sql
SELECT
  user_name, DATE_TRUNC(date, MONTH) as month, category,
  CASE category
    WHEN 'Insurance' THEN 'Fixed'
    WHEN 'Utilities' THEN 'Fixed'
    ...
    ELSE 'Variable'
  END as spend_type,
  SUM(amount) as total_amount,
  COUNT(*) as transaction_count
FROM {{ ref('stg_transactions') }}
GROUP BY 1, 2, 3, 4
```

**Dashboard Use:** Stacked bar chart showing month-by-month Fixed vs Variable breakdown

### Running dbt

```bash
cd expense_tracker_dbt

# Validate project
dbt debug

# Run all models
dbt run

# Run specific model
dbt run --select monthly_category_spend

# Run with full refresh (ignore incremental logic)
dbt run --full-refresh

# Test data quality
dbt test

# Generate documentation
dbt docs generate
dbt docs serve  # Opens local HTML documentation at http://localhost:8000
```

### dbt Project Structure

```
expense_tracker_dbt/
├── dbt_project.yml           # Project config: name, version, BigQuery settings
├── profiles.yml              # Connection profile to BigQuery (auto-generated)
├── models/
│   ├── sources.yml           # Reference to raw_statements and silver_transactions
│   ├── staging/
│   │   ├── stg_transactions.sql
│   │   └── staging.yml
│   └── marts/
│       ├── monthly_category_spend.sql
│       ├── spending_trends.sql
│       ├── spending_anomalies.sql
│       ├── recurring_expenses.sql
│       ├── merchant_insights.sql
│       ├── fixed_vs_variable_spend.sql
│       └── marts.yml         # Model config and tests
├── target/
│   ├── manifest.json         # Full DAG of models and dependencies
│   └── catalog.json          # Table metadata and lineage
└── README.md
```

---

## Stage 4: Forecasting

See [ML & Forecasting — Spend Forecasting](ml_forecasting.md#part-b-spend-forecasting-prophet).

**Summary:**
- Reads `monthly_category_spend` (Gold layer)
- Fits Prophet models per `(user, category)`
- Generates 3-month forward predictions
- Writes to `spend_forecast` BigQuery table

**Command:**
```bash
python src/analytics/forecast_monthly_spend.py
```

---

## Full Orchestration (Currently Manual)

**Current state (as of today):**

```bash
# 1. Ingest PDFs into Bronze
python src/pipeline/load_bronze_sim.py data/raw/RBC_Statement_Jan2024.pdf Kanwar Credit

# 2. Parse, enrich, deduplicate, and load Silver
python src/pipeline/load_silver.py

# 3. Run dbt to generate Gold marts
cd expense_tracker_dbt && dbt run

# 4. Generate forecasts
python src/analytics/forecast_monthly_spend.py

# 5. Launch dashboard
streamlit run src/dashboard/app.py
```

**Target state (Roadmap — Airflow/Cloud Composer):**

Combine all stages into a single Airflow DAG that runs automatically when PDFs are uploaded to GCS.

---

## Performance Considerations

### Query Optimization

- **Partitioning:** All tables partitioned by `user_name` for tenant isolation and query pruning
- **Clustering:** Consider clustering by `date` and `category` on large tables for range and filter performance
- **Materialization:** Gold marts are materialized tables, not views, for fast dashboard queries (trade-off: slower dbt run, faster analytics queries)

### Incremental Loads

**Current:** Full refresh of Silver and Gold on each run.

**Improvement:** Implement dbt incremental models to only re-process new/changed rows from Bronze.

```sql
{{
  config(
    materialized='incremental',
    unique_key='dedup_key',
    on_schema_change='ignore'
  )
}}

SELECT * FROM {{ ref('stg_transactions') }}
{% if execute %}
  WHERE ingestion_ts > (SELECT MAX(ingestion_ts) FROM {{ this }})
{% endif %}
```

### Cost Management

- **BigQuery:** Query costs are based on bytes scanned, not rows. Partitioning and clustering reduce bytes scanned.
- **Forecasting:** Prophet cross-validation can be expensive (multiple model fits). Consider running only for high-value categories or on a schedule (not every load).

---

## Data Quality & Testing

dbt supports data tests in `marts.yml`:

```yaml
models:
  - name: monthly_category_spend
    tests:
      - dbt_expectations.expect_table_row_count_to_be_between:
          min_value: 100
          max_value: 100000
    columns:
      - name: total_spend
        tests:
          - not_null
          - dbt_utils.expression_is_true:
              expression: "total_spend >= 0"  # Ensure non-negative
```

**Run tests:**
```bash
dbt test
```

---

## Troubleshooting

### Bronze Ingestion Issues

**Problem:** "PDF file not found"  
**Solution:** Ensure PDF path is absolute or relative to where the script is run. Use `os.path.abspath()`.

**Problem:** "pdfplumber cannot extract text"  
**Solution:** Some PDFs have text embedded as images (scanned statements). Use OCR (Tesseract) or request digital statements from bank.

### Silver Enrichment Issues

**Problem:** "LLM API timeout"  
**Solution:** Groq free tier has rate limits. Check `data/.llm_cache/cache.db` to see if merchant was cached. Increase timeout or use fallback provider.

**Problem:** "Deduplication removes all rows"  
**Solution:** Check that `dedup_key` is computed correctly. Ensure `date`, `merchant_standardized`, `amount`, and `user_name` are populated and correct format.

### Gold Layer Issues

**Problem:** "`dbt run` fails with BigQuery permission denied"  
**Solution:** Check GCP service account has `bigquery.admin` or `bigquery.dataEditor` role. Verify `dbt_project.yml` has correct project ID.

**Problem:** "Gold tables have null values"  
**Solution:** Some rows in Silver may have null categories or merchants. Review enrichment logs and check LLM fallback results.

### Forecasting Issues

**Problem:** "No forecasts generated"  
**Solution:** Check that `monthly_category_spend` has at least 6 months of data. Run `SELECT COUNT(DISTINCT CONCAT(user_name, category, month)) FROM monthly_category_spend` to count groups.

**Problem:** "Forecast values are negative"  
**Solution:** Clipping should handle this. Check that clipping is applied after `model.predict()`.
