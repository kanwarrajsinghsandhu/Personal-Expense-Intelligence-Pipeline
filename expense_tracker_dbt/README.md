# dbt Gold Analytics Layer — Expense Intelligence Pipeline

This directory contains dbt models that transform raw and cleaned transaction data into business-optimized analytics tables (the **Gold layer** of the Medallion architecture).

## Overview

**Purpose:** Build analytics-ready tables from Silver transactions, enabling fast dashboard queries and business intelligence.

**Input Tables:**
- `credit_card_analytics.raw_statements` (Bronze — immutable audit trail)
- `credit_card_analytics.silver_transactions` (Silver — cleaned, enriched, deduplicated)

**Output Tables:**
- 1 staging view (`stg_transactions`)
- 6 analytics marts (Gold layer)

**Technology:** dbt-bigquery (SQL + Jinja templating)

---

## Gold Analytics Marts

| Mart | Purpose | Key Columns | Materialization |
|------|---------|-----------|---|
| `monthly_category_spend` | Monthly spend aggregates per user/category | user_name, category, month, total_spend, transaction_count | Table |
| `spending_trends` | Month-over-month change analysis | user_name, category, month, mom_change, mom_change_pct | Table |
| `spending_anomalies` | Anomaly detection via Z-score | user_name, category, amount, z_score, anomaly_level | Table |
| `recurring_expenses` | Subscription & habit detection | user_name, merchant, frequency_name, annual_projection | Table |
| `merchant_insights` | Merchant-level aggregates | user_name, merchant, total_spent, visit_count, pct_of_wallet | Table |
| `fixed_vs_variable_spend` | Budget split: Essential vs Discretionary | user_name, month, spend_type, total_amount | Table |

See [Analytics & Insights](../docs/analytics_results.md) for detailed explanations.

---

## Project Structure

```
expense_tracker_dbt/
├── README.md                          # This file
├── dbt_project.yml                    # Project configuration
├── profiles.yml                       # BigQuery connection profile (auto-generated)
├── models/
│   ├── sources.yml                    # Source definitions (Bronze & Silver)
│   ├── staging/
│   │   ├── stg_transactions.sql       # Cleaning view over silver_transactions
│   │   └── staging.yml                # Column-level documentation
│   └── marts/
│       ├── monthly_category_spend.sql
│       ├── spending_trends.sql
│       ├── spending_anomalies.sql
│       ├── recurring_expenses.sql
│       ├── merchant_insights.sql
│       ├── fixed_vs_variable_spend.sql
│       └── marts.yml                  # Model configs, tests, descriptions
├── target/                            # Compiled SQL & documentation (auto-generated)
│   ├── manifest.json                  # DAG of all models
│   └── catalog.json                   # Table metadata & lineage
└── macros/                            # Reusable Jinja templates (if used)
```

---

## Quick Start

### Prerequisites

- dbt-bigquery installed (see [main README](../README.md))
- GCP service account JSON configured (via `.env`)
- `silver_transactions` table populated in BigQuery

### Running dbt

```bash
# Navigate to dbt project
cd expense_tracker_dbt

# Validate project setup
dbt debug

# Run all models (Bronze → Silver → Gold)
dbt run

# Run specific model
dbt run --select monthly_category_spend

# Full refresh (ignore incremental logic if any)
dbt run --full-refresh

# Run data quality tests
dbt test

# Generate and serve documentation
dbt docs generate
dbt docs serve  # Opens http://localhost:8000
```

### Integration with Pipeline

Typically run after Silver data is loaded:

```bash
# In project root
python src/pipeline/load_silver.py      # Populate silver_transactions
cd expense_tracker_dbt
dbt run                                  # Generate Gold marts
cd ..
python src/analytics/forecast_monthly_spend.py  # Forecasting (uses Gold tables)
```

---

## Model Details

### Staging Layer

#### `stg_transactions` (View)

**Purpose:** Clean and standardize the Silver table for analytics.

**Key Transformations:**
- Renames columns for clarity
- Coalesces `merchant_standardized` with raw `description` where missing
- Adds computed columns (e.g., `is_external = NOT is_internal`)
- Filters out internal transfers (for most analytics)
- Joins with configuration tables for category classification

**Materialization:** `view` (computed on each query, no storage cost)

### Gold Marts

#### 1. `monthly_category_spend`

Aggregates transaction amounts by (user, category, month).

**Key Logic:**
```sql
SELECT
  user_name, category,
  DATE_TRUNC(date, MONTH) as month,
  SUM(amount) as total_spend,
  COUNT(*) as transaction_count,
  AVG(amount) as avg_transaction
FROM stg_transactions
WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 36 MONTH)
  AND is_external
GROUP BY 1, 2, 3
```

**Use:** Dashboard wallets, forecasting input, trend analysis.

#### 2. `spending_trends`

Computes month-over-month change per category using LAG() window function.

**Key Logic:**
```sql
SELECT
  category, month,
  total_spend,
  LAG(total_spend) OVER (PARTITION BY category ORDER BY month) as prev_spend,
  total_spend - LAG(...) as mom_change,
  100.0 * (total_spend - LAG(...)) / LAG(...) as mom_pct_change
FROM monthly_category_spend
```

**Use:** Dashboard trend cards ("Spending is up X%"), anomaly detection.

#### 3. `spending_anomalies`

Flags transactions > 1σ or > 2σ from per-category mean.

**Key Logic:**
```sql
WITH category_stats AS (
  SELECT category,
    AVG(amount) as mean,
    STDDEV_POP(amount) as stddev
  FROM stg_transactions GROUP BY category
)
SELECT
  date, merchant, amount,
  (amount - mean) / NULLIF(stddev, 0) as z_score,
  CASE
    WHEN ABS(z_score) > 2 THEN 'High'
    WHEN ABS(z_score) > 1 THEN 'Moderate'
    ELSE 'Normal'
  END as anomaly_level
FROM stg_transactions t
LEFT JOIN category_stats s ON t.category = s.category
```

**Use:** Fraud detection, spending awareness.

#### 4. `recurring_expenses`

Detects subscriptions and habits via transaction gap analysis.

**Key Logic:**
```sql
WITH gaps AS (
  SELECT merchant,
    DATE_DIFF(date, LAG(date) OVER (PARTITION BY merchant ORDER BY date), DAY) as days_gap
  FROM stg_transactions
)
SELECT
  merchant, category,
  CASE
    WHEN days_gap BETWEEN 25 AND 35 THEN 'Monthly Subscription'
    WHEN days_gap BETWEEN 6 AND 8 THEN 'Weekly Habit'
    WHEN days_gap BETWEEN 80 AND 100 THEN 'Quarterly'
  END as frequency_name,
  AVG(amount) * (365 / AVG(days_gap)) as annual_projection
FROM gaps
WHERE frequency_name IS NOT NULL
```

**Use:** Budget tracking, subscription management, cash flow planning.

#### 5. `merchant_insights`

Aggregates spend by merchant with wallet share.

**Key Logic:**
```sql
SELECT
  merchant, category,
  SUM(amount) as total_spent,
  COUNT(*) as visit_count,
  100.0 * SUM(amount) / SUM(SUM(amount)) OVER () as pct_of_wallet
FROM stg_transactions
WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 12 MONTH)
GROUP BY 1, 2
ORDER BY total_spent DESC
```

**Use:** Spending pattern recognition, top merchants, negotiation targets.

#### 6. `fixed_vs_variable_spend`

Classifies categories as Fixed/Essential, Variable/Discretionary, or Transfers.

**Key Logic:**
```sql
SELECT
  DATE_TRUNC(date, MONTH) as month,
  category,
  CASE
    WHEN category IN ('Insurance', 'Utilities', 'Rent') THEN 'Fixed'
    WHEN category IN ('Dining', 'Shopping', 'Entertainment') THEN 'Variable'
    ELSE 'Transfer'
  END as spend_type,
  SUM(amount) as total_amount
FROM stg_transactions
GROUP BY 1, 2, 3
```

**Use:** Budget planning, discretionary spending analysis.

---

## Data Quality Tests

Tests are defined in `marts.yml`:

```yaml
models:
  - name: monthly_category_spend
    tests:
      - dbt_expectations.expect_table_row_count_to_be_between:
          min_value: 100
    columns:
      - name: total_spend
        tests:
          - not_null
          - dbt_utils.expression_is_true:
              expression: "total_spend >= 0"
```

**Run tests:**
```bash
dbt test
```

---

## Documentation & Lineage

dbt automatically generates documentation from YAML configs:

```bash
dbt docs generate
dbt docs serve
```

Opens at `http://localhost:8000` with:
- Model lineage DAG (visual dependency graph)
- Column-level documentation
- Data types and transformations
- Test results and coverage

---

## Performance Tips

### Partitioning

All Gold tables should be partitioned by `user_name` (tenant) and `date` for efficient querying:

```yaml
{{
  config(
    materialized = 'table',
    partition_by = {
      'field': 'date',
      'data_type': 'date',
      'granularity': 'month'
    }
  )
}}
```

### Clustering

Consider clustering by frequently-filtered columns:

```yaml
{{
  config(
    materialized = 'table',
    cluster_by = ['user_name', 'category']
  )
}}
```

### Incremental Materialization (Future)

For large tables, use dbt incremental models to avoid full refreshes:

```yaml
{{
  config(
    materialized = 'incremental',
    unique_key = 'dedup_key'
  )
}}

SELECT *
FROM {{ ref('stg_transactions') }}
{% if execute %}
  WHERE ingestion_ts > (SELECT MAX(ingestion_ts) FROM {{ this }})
{% endif %}
```

---

## Troubleshooting

### Connection Errors

**Problem:** `dbt debug` fails with "permission denied"  
**Solution:** Verify GCP service account has `bigquery.admin` role. Check `~/.dbt/profiles.yml` is configured.

### Model Failures

**Problem:** `dbt run` fails on a specific model  
**Solution:** Check table dependencies. Run parent models first:
```bash
dbt run --select +my_failing_model  # Run dependencies first
```

### NULL Values

**Problem:** Gold tables have unexpected NULLs  
**Solution:** Check upstream Silver table for data quality issues. Review `dbt test` results.

---

## Resources

- [dbt Documentation](https://docs.getdbt.com/docs/introduction)
- [dbt BigQuery Plugin](https://docs.getdbt.com/reference/warehouse-setups/bigquery-setup)
- [Main Project README](../README.md)
- [Data Pipeline Details](../docs/data_pipeline.md)
- [Analytics & Insights](../docs/analytics_results.md)

---

## Next Steps

1. ✅ Run `dbt run` to generate Gold tables
2. 📊 Query marts in BigQuery for analysis
3. 📈 Connect Streamlit dashboard to Gold layer
4. 🧪 Add more tests in `marts.yml`
5. 📚 Expand documentation via YAML configs

---

**Status:** Production-ready | **Last Updated:** June 2024
