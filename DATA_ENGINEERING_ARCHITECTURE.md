# 🏗 Industry-Grade Data Engineering Architecture: Expense Tracker

This document outlines the current state and future roadmap of the **Multi-Tenant Expense Intelligence Pipeline**.

---

## 📍 Current Architecture: The Lakehouse Pattern
We have moved from a local notebook to a professional **Medallion Architecture** (Bronze ➡️ Silver ➡️ Gold) hosted on Google Cloud Platform (BigQuery).

```mermaid
graph TD
    subgraph "1. Ingestion (Bronze)"
        PDF[PDF Statements] -->|Python CLI| BQ_BRONZE[BQ: raw_statements]
        META[Metadata: User/Bank/Type] --> BQ_BRONZE
    end

    subgraph "2. Transformation & Enrichment (Silver)"
        BQ_BRONZE -->|Python + LLM| BQ_SILVER[BQ: silver_transactions]
        CAT[Merchant Catalog] --> BQ_SILVER
        GROQ[Groq/LLM Fallback] --> BQ_SILVER
        DEDUP[SHA256 Deduplication] --> BQ_SILVER
    end

    subgraph "3. Modeling & Analytics (Gold)"
        BQ_SILVER -->|dbt Staging| STG[stg_transactions]
        STG -->|dbt Marts| MARTS[Monthly Spend / Trends / Anomalies]
    end

    subgraph "4. Consumption"
        MARTS -->|SQL| LOOKER[Looker Studio Dashboard]
        MARTS -->|Filter| STREAMLIT[Streamlit App (Future)]
    end
```

---

## ✅ Completed Milestones

### **Bronze Layer (Raw Storage)**
- **Immutable Audit Trail**: Stores raw PDF text as JSON in BigQuery.
- **Dynamic Metadata**: Captures `owner_name`, `statement_type`, and `bank_name` at the moment of ingestion via CLI arguments.

### **Silver Layer (The Intelligence Layer)**
- **Multi-Tenant Support**: Every transaction row is tagged with its owner and source bank.
- **SHA256 Deduplication**: A composite hash of `(date + merchant + amount + user)` ensures zero double-counting, even if pipelines are re-run.
- **Hybrid Enrichment**: 
    1. Fuzzy matching against a manual `merchant_catalog.json`.
    2. LLM fallback (Groq) for unknown merchants.
    3. Merchant standardization (e.g., "AMZN MKTP" ➡️ "Amazon").

### **Gold Layer (Business Value via dbt)**
- **Staging**: Standardizes column names and types.
- **Marts**: Specialized tables for:
    - **Retention**: Recurring subscription detection.
    - **Risk**: Anomaly detection (2+ Standard Deviations).
    - **Growth**: Month-over-Month spending trends and "Percent of Wallet" analysis.

---

## 🛠 Technology Stack

| Component | Technology | Role |
| :--- | :--- | :--- |
| **Storage** | Google BigQuery | Distributed data warehouse. |
| **Logic** | Python 3.11 | PDF extraction and LLM orchestration. |
| **Transformation** | dbt (Data Build Tool) | SQL modeling and lineage management. |
| **Intelligence** | Groq (Llama 3) | Merchant classification and standardization. |
| **Environment** | python-dotenv | Secure secret management (API keys). |

---

## 🚀 Future Roadmap: Scaling & Automation

The project is currently structured for **Production Deployment**. Because we have decoupled the logic (Python), transformations (dbt), and secrets (.env), the following can be added incrementally:

### 1. **Orchestration (Airflow / Dagster)**
- **What**: Automate the sequence `Python Load -> dbt run -> dbt test`.
- **Why**: Currently you run these manually. Airflow will trigger them automatically when a file is dropped into a folder.
- **Status**: Ready to implement. The Python scripts already use CLI arguments, which Airflow can easily pass.

### 2. **CI/CD (GitHub Actions)**
- **What**: Automate testing when you push code to GitHub.
- **Action**:
    - Run `pytest` on your parsing logic.
    - Run `dbt test` to ensure no NULL values or duplicates.
    - Deploy dbt docs to GitHub Pages.

### 3. **Cloud Native Ingestion**
- **What**: Move from local `python` runs to **GCP Cloud Functions**.
- **Flow**: Upload PDF to Google Cloud Storage ➡️ Trigger Cloud Function ➡️ Auto-Bronze ➡️ Auto-Silver.

### 4. **Consumption Layer**
- **Looker Studio**: Build a multi-page dashboard with a `user_name` filter.
- **Streamlit**: A simple web app where users can upload PDFs and see their specific Gold results.

---

## 🧠 Data Engineer Observations
The architecture now handles **Deduplication** and **Tenant Isolation**. This means you can onboard multiple users (James, Kanwar, etc.) onto the same infrastructure without data leakage—a key requirement for any real-world Fintech application.
