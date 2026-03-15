# 💳 Expense Intelligence Pipeline

A production-grade, multi-tenant Data Engineering pipeline that transforms raw bank statement PDFs into deep financial insights using **Python, dbt, BigQuery, and LLMs (Groq)**.

---

## 🏗 High-Level Architecture
- **Bronze Layer**: Raw PDF text extraction & storage in BigQuery.
- **Silver Layer**: Data cleaning, SHA256 deduplication, and hybrid enrichment (ML + Catalog + LLM Standardization).
- **Gold Layer**: Advanced analytics via dbt (Aggregation, Anomaly Detection, Subscription Analysis).

---

## 🚀 Getting Started

### 1. Prerequisites
- Python 3.9+
- A Google Cloud Project with BigQuery enabled.
- A Groq API Key (for LLM standardization).

### 2. Installation
```bash
# Clone the repository
git clone <your-repo-url>
cd Expense_Tracker

# Create and activate virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install -e .
```

### 3. Configuration
Create a `.env` file in the root directory:
```env
GOOGLE_APPLICATION_CREDENTIALS="path/to/your/service-account.json"
GROQ_API_KEY="your-groq-api-key"
```

---

## 🔄 Running the Pipeline

### Step 1: Ingest Raw PDF (Bronze)
This is a multi-tenant pipeline. You must specify the owner and statement type.
```bash
python -m src.pipeline.load_bronze_sim --file "data/raw/your-statement.pdf" --user "John" --type "Credit"
```

### Step 2: Enrich & Deduplicate (Silver)
Processes the raw data into structured transactions.
```bash
python -m src.pipeline.load_silver
```

### Step 3: Run Analytics (Gold)
Generate all 7 business models in BigQuery.
```bash
cd expense_tracker_dbt
dbt run
```

---

## 📊 Gold Layer Insights
Once the pipeline finishes, you will have these tables ready in BigQuery:
- `stg_transactions`: Cleaned individual records.
- `monthly_category_spend`: High-level budget summaries.
- `spending_anomalies`: Flags transactions > 2 standard deviations from your norm.
- `recurring_expenses`: Automatically detects ghost subscriptions (Netflix, Spotify, etc.).
- `merchant_insights`: Brand-level concentration and % of wallet.

---

## 🔐 Security Note
- **DO NOT** commit your `.env` or `Keys/` folder to GitHub. The included `.gitignore` protects these assets.
- Raw PDFs in `data/raw/` are automatically ignored by Git.
