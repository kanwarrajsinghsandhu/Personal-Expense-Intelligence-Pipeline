# Machine Learning & Forecasting

This document covers the two ML subsystems in the Expense Intelligence Pipeline:
1. **Transaction Categorization** — multi-stage cascade to assign category/subcategory to each transaction
2. **Spend Forecasting** — Prophet time-series models for 3-month forward predictions

---

## Part A: Transaction Categorization (4-Stage Cascade)

### Overview

Each transaction's merchant description must be mapped to a canonical `(category, subcategory)` pair. This is expensive to do via LLM alone, so we use a cost-optimized 4-stage waterfall:

```
Raw Merchant Description
    ↓
Stage 1: Regex Heuristics (0 cost, ~30% match rate)
    ├→ Match: Return result
    └→ No match: continue
    ↓
Stage 2: Fuzzy Catalog Lookup (0 cost, ~50% match rate)
    ├→ Match: Return result
    └→ No match: continue
    ↓
Stage 3: ML Classifier (0 cost, ~15% match rate)
    ├→ Match: Return result
    └→ No match: continue
    ↓
Stage 4: LLM Fallback (API cost, ~5% match rate)
    └→ Return LLM result (cached in SQLite)
```

**Total Coverage:** ~100% (every merchant gets categorized).  
**Cost:** Only ~5% of merchants require API calls (LLM).

---

### Stage 1: Regex Heuristics

**File:** `src/enrich.py` — `_REGEX_RULES` dictionary  
**Coverage:** ~30% of transactions

Canonical patterns for major Canadian merchants. Examples:

```python
_REGEX_RULES = {
    r'TIM HORTON': ('Food & Dining', 'Coffee Shops'),
    r'ROGERS|TELUS|BELL': ('Utilities', 'Telecom'),
    r'NETFLIX|SPOTIFY|APPLE|ADOBE': ('Subscriptions', 'Digital Services'),
    r'AMAZON\.COM|AMZN': ('Shopping', 'Online Retail'),
    r'COSTCO|WALMART|LOBLAWS': ('Shopping', 'Groceries'),
    r'INTERAC|BMO|RBC': ('Transfers', 'Internal'),
    r'PAYMENT|PMT': ('Transfers', 'Bill Payment'),
    # ... ~50+ more rules
}
```

**Matching:** Case-insensitive substring match. If merchant description contains any regex pattern, that rule is applied.

**Pros:**
- Zero latency, zero cost
- Human-readable (easy to add new merchants)
- Deterministic (no randomness)

**Cons:**
- Requires manual rule writing
- Cannot handle typos or variations

---

### Stage 2: Fuzzy Catalog Lookup

**File:** `src/enrich.py` — loads `data/merchant_catalog.json`  
**Coverage:** ~50% of transactions (includes Stage 1 hits + new matches)

A curated merchant catalog mapping canonical merchant names to `(category, subcategory)`:

```json
{
  "tim hortons": {"category": "Food & Dining", "subcategory": "Coffee Shops"},
  "costco": {"category": "Shopping", "subcategory": "Warehouse Clubs"},
  "amazon": {"category": "Shopping", "subcategory": "Online Retail"},
  ...
}
```

**Matching:** Uses `rapidfuzz.fuzz.token_set_ratio()` with a 70% similarity threshold. Allows typos and abbreviations (e.g., "THD" matches "The Home Depot").

**Pros:**
- Tolerates typos and abbreviations
- Curated reference (high accuracy)
- Zero cost

**Cons:**
- Requires maintaining a catalog
- Limited to known merchants

---

### Stage 3: ML Classifier (SentenceTransformer + RandomForest)

**Files:** 
- Training: `src/1_train_model.py`
- Inference: `src/enrich.py` — loaded from `models/category_classifier.pkl` and `models/subcategory_classifier.pkl`

**Coverage:** ~15% of transactions (new merchants unseen in Stages 1–2)

#### Training Pipeline

**Data:** `data/trainings/FinalTransaction.csv` (labeled transactions with `raw_text`, `category`, `subcategory` columns)

**Steps:**

1. **Vectorization:** Convert merchant text to 384-dimensional embeddings using `sentence-transformers/all-MiniLM-L6-v2`
   - Model: Hugging Face pre-trained DistilBERT-based sentence encoder
   - Dimension: 384
   - Inference speed: ~1ms per merchant (on CPU)

2. **Encoding:** Apply `LabelEncoder` to category and subcategory strings (e.g., "Food & Dining" → 0, "Shopping" → 1, etc.)

3. **Train/Test Split:** 80/20 stratified split (preserves class distribution)

4. **Model Training:** Fit two independent `RandomForestClassifier` models:
   - Category classifier: classifies raw merchant text → category
   - Subcategory classifier: classifies raw merchant text → subcategory
   - Hyperparameters: `n_estimators=100`, `max_depth=20`, `random_state=42`, `n_jobs=-1`

5. **Evaluation:** Print classification reports (precision, recall, F1) on test set

6. **Serialization:** Save to `models/`:
   - `category_classifier.pkl` — RandomForest model for category
   - `subcategory_classifier.pkl` — RandomForest model for subcategory
   - `category_encoder.pkl` — LabelEncoder for category strings
   - `subcategory_encoder.pkl` — LabelEncoder for subcategory strings

#### Inference

```python
# Load pre-trained artifacts
classifier = joblib.load('models/category_classifier.pkl')
encoder = joblib.load('models/category_encoder.pkl')

# Vectorize merchant text
embeddings = model.encode([merchant_text])  # Shape: (1, 384)

# Predict
category_idx = classifier.predict(embeddings)[0]
category = encoder.inverse_transform([category_idx])[0]
```

**Accuracy:** Depends on training data quality. Typical F1 scores: 0.85–0.92 per category.

**Pros:**
- Fast (no API calls, pure local inference)
- High accuracy on trained categories
- Generalizes to unseen merchant text via embeddings

**Cons:**
- Requires labeled training data
- Cannot discover new categories (limited to training set)
- Cannot handle truly novel merchants (e.g., foreign merchants, new businesses)

---

### Stage 4: LLM Fallback (Groq Llama 3 + Cache)

**File:** `src/llm_client.py`  
**Coverage:** ~5% of transactions (merchants not matched in Stages 1–3)

For truly novel or ambiguous merchants, invoke an LLM with a structured prompt.

#### LLM Provider Options

**Primary:** Groq (free tier, Llama 3 8B)
- Fast (sub-second latency)
- Free (within rate limits)
- API: `https://api.groq.com/openai/v1/chat/completions`

**Fallback options (in code):**
- OpenAI GPT-4 / GPT-3.5 Turbo
- Google Gemini

#### Caching Strategy

LLM results are cached in SQLite (`data/.llm_cache/cache.db`) to avoid re-calling the API for the same merchant string.

**Cache entry:** `{ merchant_text → (category, subcategory, llm_response_json) }`

**Lookup:** Before calling LLM, check if merchant text exists in cache. If yes, return cached result (instant). If no, call LLM, store result, return.

**Cache hit rate:** ~90% (most merchants appear multiple times across statement months).

#### Prompt Design

```
You are a financial categorization assistant. 
Given a merchant description from a bank statement, 
classify it into one of these predefined categories and subcategories.

Merchant: "{merchant_text}"

Respond ONLY in valid JSON format:
{
  "category": "<one of: Food & Dining, Subscriptions, Shopping, ...>",
  "subcategory": "<one of: Coffee Shops, Digital Services, Online Retail, ...>",
  "confidence": 0.0–1.0,
  "reasoning": "brief explanation"
}
```

**Parsing:** Extract JSON from response; ignore text outside JSON block.

**Fallback:** If LLM response is malformed, default to ("Unknown", "Other").

#### Cost & Rate Limits

- **Groq:** Free tier: 30 requests/min (shared), pay-as-you-go after
- **OpenAI:** $0.50–2.00 per 1M input tokens (GPT-4 is more expensive)
- **Expected cost:** ~5% of transactions × average merchant text size = minimal (< $1/month)

**Mitigation:** Cache hit rate is high; API calls are rare.

---

## Part B: Spend Forecasting (Prophet)

### Overview

Forecasting predicts future monthly spending per category. Used in the Streamlit dashboard's "Predictions" page.

**Model:** Facebook Prophet (additive time-series decomposition)  
**Input:** Monthly category spend aggregated from `monthly_category_spend` dbt mart  
**Output:** 3-month forward predictions written to `spend_forecast` BigQuery table  
**Execution:** Manual Python script (not yet automated in Airflow)

---

### Data Preparation

**Source:** `monthly_category_spend` Gold mart table in BigQuery

```sql
SELECT
  user_name,
  category,
  year_month,
  SUM(amount) as total_spend,
  COUNT(*) as transaction_count
FROM silver_transactions
WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 36 MONTH)
  AND user_name = 'Kanwar'
GROUP BY user_name, category, year_month
```

**Key Insight:** Query returns one row per `(user, category, month)` pair.

#### Data Densification (Fill Missing Months)

**Problem:** If a user has no "Gas" transactions in February, that month is absent from query results. Prophet would see this as data discontinuity (gap in the timeline).

**Solution:** Fill missing months with $0 spend.

```python
# Original: [(Jan: $100), (Feb: MISSING), (Mar: $50)]
# Densified: [(Jan: $100), (Feb: $0), (Mar: $50)]
```

**Implementation:** `pandas.date_range()` to generate all months between min and max, then `reindex()` with `fill_value=0`.

**Effect:** Tells Prophet "this category had zero spend in February", which is correct.

---

### Model Fitting

**File:** `src/analytics/forecast_monthly_spend.py`

#### Minimum Data Requirement

**6 months minimum:** If a `(user, category)` pair has fewer than 6 months of history, it is skipped (no forecast generated).

**Rationale:** Prophet needs sufficient history to estimate trend and seasonality. 6 months is a conservative floor.

```python
if len(category_data) < 6:
    print(f"Skipping {user}/{category}: insufficient data ({len(category_data)} months)")
    continue
```

#### Model Configuration

```python
model = Prophet(
    yearly_seasonality=True,      # Enable annual seasonality (holiday spending patterns)
    weekly_seasonality=False,     # Disable (data is monthly, not daily)
    daily_seasonality=False,
    changepoint_prior_scale=0.3,  # Moderate trend flexibility
    interval_width=0.80,          # 80% confidence interval
)

# Add Canadian holidays (Boxing Day, Family Day, etc.)
model.add_country_holidays('CA')

# Fit
with warnings.catch_warnings():
    warnings.filterwarnings('ignore')
    model.fit(df)
```

**Key Parameters:**

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `yearly_seasonality` | `True` | Capture annual spending patterns (e.g., higher in Dec for holidays) |
| `weekly_seasonality` | `False` | Monthly data has no weekly pattern; disable to reduce overfitting |
| `changepoint_prior_scale` | 0.3 | Allows up to 25 trend breakpoints; 0.3 is moderate (not too rigid, not too flexible) |
| `interval_width` | 0.80 | 80% confidence interval (yhat_lower, yhat_upper) |
| `uncertainty_samples` | 100 | 100 posterior sample draws for CI estimation |

#### Cross-Validation (18+ Months Only)

If `len(category_data) >= 18`, run Prophet's built-in cross-validation:

```python
cv_results = cross_validation(
    model,
    initial='365 days',    # Train on first 365 days
    period='30 days',      # Step through 30 days at a time
    horizon='90 days',     # Evaluate on next 90 days
    parallel=None,
)

# Compute metrics
metrics_df = compute_metrics(cv_results)
mape = metrics_df['mape'].mean()
rmse = metrics_df['rmse'].mean()
mae = metrics_df['mae'].mean()

print(f"{user} / {category}: MAPE={mape:.2%}, RMSE=${rmse:.2f}, MAE=${mae:.2f}")
```

**Metrics:**
- **MAPE** (Mean Absolute Percentage Error): % error relative to actual spend
- **RMSE** (Root Mean Squared Error): penalizes large errors more heavily
- **MAE** (Mean Absolute Error): average absolute error in dollars

---

### Forecast Generation

**Forecast Horizon:** 3 months into the future

```python
future = model.make_future_dataframe(periods=3, freq='MS')  # Month start
forecast = model.predict(future)

# Output columns: yhat, yhat_lower, yhat_upper, trend, yearly, holidays
```

**Clipping:** All predictions are floored at $0 (no negative spend):

```python
forecast['yhat'] = forecast['yhat'].clip(lower=0)
forecast['yhat_lower'] = forecast['yhat_lower'].clip(lower=0)
forecast['yhat_upper'] = forecast['yhat_upper'].clip(lower=0)
```

---

### Writing to BigQuery

**Table:** `spend_forecast` (fully replaced on each run)

**Schema:**
```sql
CREATE TABLE credit_card_analytics.spend_forecast (
  user_name STRING,
  category STRING,
  forecast_date DATE,
  predicted_amount FLOAT64,
  predicted_lower FLOAT64,           -- 80% CI lower bound
  predicted_upper FLOAT64,           -- 80% CI upper bound
  forecast_generated_ts TIMESTAMP,
  data_density_pct FLOAT64,          -- % of months with data (0–100)
  cv_mape FLOAT64 NULL,              -- Cross-validation MAPE (if 18+ months)
  cv_rmse FLOAT64 NULL,              -- Cross-validation RMSE
  cv_mae FLOAT64 NULL,               -- Cross-validation MAE
);
```

**Write Mode:** `WRITE_TRUNCATE` (full refresh, not append)

**Loading:** BigQuery Python client `load_table_from_dataframe()` with `job_config.write_disposition='WRITE_TRUNCATE'`

---

### Dashboard Integration

The Streamlit dashboard queries `spend_forecast` and displays:

1. **Category forecast chart:** Historical trend (from `monthly_category_spend`) bridged to future predictions (from `spend_forecast`). Shaded confidence interval band.
2. **Forecast table:** One row per category per forecast month, showing `predicted_amount`, `predicted_lower`, `predicted_upper`.
3. **Total wallet outlook:** Aggregated forecast across all categories.

**Interactivity:** Sidebar slider to select forecast horizon (1–3 months); dashboard dynamically filters forecast table to selected range.

---

## Running the ML Pipeline

### Training (One-Time)

```bash
python src/1_train_model.py
```

**Output:** Trained sklearn models saved to `models/`

**When to retrain:**
- After collecting significantly more labeled data
- If classification accuracy degrades
- Quarterly or semi-annually for performance tuning

### Categorization Enrichment (Per Silver Load)

```bash
# Automatically invoked by load_silver.py
python src/pipeline/load_silver.py
```

Calls enrichment cascade internally.

### Forecasting (After Gold Layer)

```bash
# After dbt has run and monthly_category_spend is current
python src/analytics/forecast_monthly_spend.py
```

**Typical runtime:** 30–60 seconds for all user/category combinations.

---

## Next Steps & Improvements

1. **Category Discovery:** Allow Prophet or ML model to propose new categories based on merchant clustering
2. **Ensemble Forecasting:** Combine Prophet with ARIMA, Exponential Smoothing, or ML-based forecasts (XGBoost, LightGBM)
3. **Causal Inference:** Model external factors (holidays, promotions, life events) affecting spend
4. **Transfer Learning:** Fine-tune sentence-transformers on financial merchant text for better embeddings
5. **Active Learning:** Suggest uncertain transactions to the user for labeling, retrain models incrementally
6. **Automated Retraining:** Trigger ML model retraining on a schedule (monthly) or when classification accuracy drops below a threshold
