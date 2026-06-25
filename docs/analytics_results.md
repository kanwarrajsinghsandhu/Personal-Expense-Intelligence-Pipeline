# Analytics & Insights

This document describes the analytical insights and metrics produced by the Expense Intelligence Pipeline.

---

## Overview

The Gold analytics layer generates **6 integrated marts** that answer key personal finance questions:

| Question | Mart | Key Metric |
|----------|------|-----------|
| How much am I spending per category? | `monthly_category_spend` | Monthly aggregate by category |
| Is my spending going up or down? | `spending_trends` | Month-over-month change % |
| Which transactions are unusual? | `spending_anomalies` | Z-score based anomaly flags |
| What subscriptions do I have? | `recurring_expenses` | Detected recurring patterns with annual projection |
| Which merchants do I spend most on? | `merchant_insights` | Top merchants by total spend and % of wallet |
| How much is fixed vs discretionary? | `fixed_vs_variable_spend` | Split spend into Essential/Variable/Transfer |

All metrics are **tenant-isolated** (per `user_name`) and **time-windowed** (typically last 12 months for trending, last 36 months for long-term analysis).

---

## 1. Wallet Share & Category Breakdown

### What It Measures

Total spend distribution across categories over a rolling 12-month window.

### Source

Aggregated from `monthly_category_spend` Gold mart, summed across all months in the trailing year.

### Query Example

```sql
SELECT
  category,
  SUM(total_spend) as category_total,
  100.0 * SUM(total_spend) / SUM(SUM(total_spend)) OVER () as pct_of_wallet
FROM credit_card_analytics.monthly_category_spend
WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 12 MONTH)
  AND user_name = 'Kanwar'
GROUP BY category
ORDER BY category_total DESC
```

### Dashboard Visualization

**Donut chart** showing category slices. Hovering over a slice reveals:
- Category name
- Total spend in period
- Percentage of wallet
- Average monthly spend in category

### Interpretation

- **High % categories** (>30%): Major spending areas. Opportunities for optimization if discretionary.
- **Low % categories** (<2%): Niche spending. May indicate one-off purchases or small occasional expenses.
- **Category absence:** No transactions in that category during the period (either not applicable or deferred).

### Example Output

```
Food & Dining:         $3,500     18%
Shopping:              $4,200     22%
Utilities:             $2,100     11%
Subscriptions:         $1,800     9%
Transfers:             $7,500     39%
Other:                 $800       1%
                       -------
Total:                 $19,900    100%
```

---

## 2. Fixed vs. Variable Expenditures

### What It Measures

Spend categorized into:
- **Fixed/Essential:** Non-discretionary spending (rent, insurance, utilities, subscriptions)
- **Variable/Discretionary:** Discretionary spending (dining, shopping, entertainment)
- **Transfers:** Payments, bill payments, internal transfers

### Classification Rules

```
Fixed/Essential:
  ├── Insurance (Health, Auto, Home, Life)
  ├── Utilities (Internet, Phone, Gas, Electricity, Water)
  ├── Rent (Housing)
  ├── Subscriptions (Digital Services, Streaming)
  └── Communications

Variable/Discretionary:
  ├── Food & Dining (Restaurants, Coffee, Groceries)
  ├── Shopping (General, Clothing, Groceries)
  ├── Entertainment (Movies, Games, Events)
  ├── Travel (Gas, Hotels, Flights)
  └── Personal (Haircuts, Gym, Books)

Transfers:
  ├── Internal (payments between own accounts)
  └── Bill Payments
```

**Customization:** The classification is hardcoded in `fixed_vs_variable_spend.sql` and can be adjusted per user preferences.

### Source

`fixed_vs_variable_spend` Gold mart, aggregated by month and spend_type.

### Dashboard Visualization

**Stacked bar chart** (time series) showing Fixed vs Variable spend over the last 12 months.

**Interpretation:**
- **High Fixed %:** Committed obligations dominate budget. Less flexibility.
- **High Variable %:** More discretionary control; easier to cut spending if needed.
- **Trend:** Are obligations growing (more subscriptions)? Is discretionary spending increasing?

### Example Output

```
Month       Fixed/Essential  Variable/Discretionary  Transfers  Total
Jan 2024    $800 (40%)      $700 (35%)              $500 (25%)  $2,000
Feb 2024    $820 (41%)      $680 (34%)              $500 (25%)  $2,000
Mar 2024    $850 (42%)      $720 (35%)              $430 (21%)  $2,000
...
Dec 2024    $1,200 (50%)    $900 (38%)              $300 (12%)  $2,400
```

**Insight:** Fixed spend is growing; discretionary is stable. Consider whether new subscriptions are justified.

---

## 3. Adaptive Spend Anomalies

### What It Measures

Transactions flagged as statistically unusual within each category.

### Detection Methodology

**Step 1:** Compute per-category statistics (mean and standard deviation):
```sql
category_mean = MEAN(amount) for all transactions in category
category_stddev = STDDEV_POP(amount) for all transactions in category
```

**Step 2:** Calculate Z-score for each transaction:
```
z_score = (transaction_amount - category_mean) / category_stddev
```

**Step 3:** Classify anomaly level:

| Z-Score Range | Anomaly Level | Interpretation |
|---|---|---|
| -1 to +1 | Normal | Expected variation (68% of data) |
| -2 to -1 or +1 to +2 | Moderate Anomaly | Unusually high or low (16–32% of data) |
| < -2 or > +2 | High Anomaly | Outlier (< 5% of data) |

### Source

`spending_anomalies` Gold mart with Z-score and anomaly_level computed.

### Dashboard Visualization

**Anomalies table** showing top 20 High Anomaly transactions in reverse chronological order:

Columns:
- Date
- Merchant
- Category
- Amount
- Category Mean
- Z-Score
- Anomaly Level

### Use Cases

**Fraud Detection:** High Anomaly transactions warrant review (unusual amount, unusual merchant for category).

**Spending Awareness:** Moderate Anomalies signal moments when you spent significantly more or less than usual in a category.

**Example:**
- Normal Groceries spending: $50–100
- Costco trip: $300 → **Moderate Anomaly** (within 2σ, but notably larger)
- Accidental duplicate charge: $150 → **High Anomaly** (> 2σ away from mean)

### Example Output

```
Date        Merchant        Category    Amount  Cat Mean  Z-Score  Level
2024-12-20  Costco Whole    Shopping    $350    $75       +2.2     High
2024-12-15  Amazon          Shopping    $200    $75       +1.5     Moderate
2024-11-01  Coffee Shop     Dining      $45     $8        +4.6     High
2024-10-22  Grocery Store   Food        $25     $65       -1.8     Moderate
```

---

## 4. Recurring Subscription Commitments

### What It Measures

Transactions with regular intervals (weekly, monthly, quarterly) indicating subscriptions or habits.

### Detection Methodology

**Step 1:** For each `(user, merchant)` pair, sort transactions by date.

**Step 2:** Compute gaps between consecutive transactions:
```sql
gap_in_days = DATE_DIFF(current_date, previous_date, DAY)
```

**Step 3:** Classify by gap pattern:

| Gap Range | Pattern | Frequency | Annualized |
|---|---|---|---|
| 25–35 days | Monthly Subscription | 12× per year | amount × 12 |
| 6–8 days | Weekly Habit | 52× per year | amount × 52 |
| 80–100 days | Quarterly | 4× per year | amount × 4 |

**Threshold rationale:** Real-world subscriptions vary (28–31 days for monthly; 6–8 days for weekly) due to calendar variation and billing date drift.

### Source

`recurring_expenses` Gold mart with occurrence_count and annual_projection.

### Dashboard Visualization

**Recurring Expenses table** sorted by annual_projection (highest first):

Columns:
- Merchant
- Category
- Frequency
- Average Amount
- Last Transaction Date
- Annual Projection

### Use Cases

**Budget Planning:** Know your committed monthly/annual obligations.

**Cost Reduction:** Review subscriptions for services no longer used; cancel unused subscriptions.

**Cash Flow Planning:** Forecast fixed cash outflows per month.

### Example Output

```
Merchant                 Category         Frequency    Avg Amount  Annual Proj
Netflix                  Subscriptions    Monthly      $17         $204
Spotify                  Subscriptions    Monthly      $12         $144
Planet Fitness           Fitness          Monthly      $30         $360
Coffee Shop (daily)      Food & Dining    Weekly       $7          $364
Grocery Store            Shopping         Weekly       $85         $4,420
Amazon Prime             Subscriptions    Yearly       $149        $149
                                                       Total:      $5,641/yr
```

**Insight:** ~$470/month in recurring commitments, with $364 in daily coffee (discretionary opportunity).

---

## 5. Merchant Insights

### What It Measures

Per-merchant spending aggregates: total spend, visit frequency, and share of wallet.

### Source

`merchant_insights` Gold mart, aggregated from Silver transactions in trailing 12 months.

### Query Example

```sql
SELECT
  merchant_standardized,
  category,
  SUM(amount) as total_spent,
  COUNT(*) as visit_count,
  100.0 * SUM(amount) / SUM(SUM(amount)) OVER (PARTITION BY user_name) as pct_of_wallet,
  AVG(amount) as avg_transaction,
  MIN(date) as first_visit,
  MAX(date) as last_visit
FROM credit_card_analytics.merchant_insights
WHERE user_name = 'Kanwar'
  AND date >= DATE_SUB(CURRENT_DATE(), INTERVAL 12 MONTH)
GROUP BY merchant_standardized, category
ORDER BY total_spent DESC
LIMIT 50
```

### Dashboard Visualization

**Top Merchants Chart** (horizontal bar or table):

Columns:
- Rank
- Merchant
- Category
- Total Spend
- Visit Count
- % of Wallet
- Avg Transaction

### Use Cases

**Spending Pattern Recognition:** Where is your money going? Identify largest merchants.

**Merchant Negotiation:** High-spend merchants (e.g., utilities, insurance) are candidates for rate negotiation.

**Loyalty Program Opportunities:** See where you spend regularly; enroll in loyalty programs.

### Example Output

```
Rank  Merchant           Category      Total   Visits  % Wallet  Avg
1     Whole Foods        Shopping      $2,100  52      10.5%     $40
2     Shell Gas          Travel        $1,800  36      9.0%      $50
3     Rogers Wireless    Telecom       $1,600  12      8.0%      $133
4     Amazon.com         Shopping      $1,400  35      7.0%      $40
5     Starbucks          Food & Dining $1,200  150     6.0%      $8
...
50    Netflix            Subscriptions $150    12      0.75%     $13
```

**Insight:** Top 5 merchants account for ~40% of wallet. Opportunities: shop at lower-cost grocer, optimize gas spending, negotiate wireless plan.

---

## 6. Spending Trends (Month-over-Month Analysis)

### What It Measures

Month-over-month change in category spending (absolute and percentage).

### Source

`spending_trends` Gold mart with LAG() to compute previous month's values.

### Query Example

```sql
SELECT
  category,
  month,
  total_spend,
  LAG(total_spend) OVER (PARTITION BY category ORDER BY month) as prev_month_spend,
  total_spend - LAG(total_spend) OVER (...) as mom_change,
  100.0 * (total_spend - LAG(total_spend) OVER (...)) / LAG(total_spend) OVER (...) as mom_change_pct
FROM credit_card_analytics.spending_trends
WHERE user_name = 'Kanwar'
```

### Dashboard Visualization

**Trend Cards** (top of dashboard) showing:
- Latest month's spend
- MoM absolute change ($)
- MoM percentage change (%)
- **Visual indicator:** ↑ green (up), ↓ red (down), → neutral

**Line Chart** showing historical trend per category (optional).

### Use Cases

**Trend Spotting:** Is spending accelerating or decelerating?

**Anomaly Investigation:** Sudden spike in a category warrants investigation.

**Goal Tracking:** Compare against personal budget targets.

### Example Output

```
Category           Latest Month  MoM Change  MoM %   Indicator
Food & Dining      $350          +$50        +16.7%  ↑ (increased)
Shopping           $400          -$75        -15.8%  ↓ (decreased)
Subscriptions      $50           +$0         +0.0%   → (stable)
Travel             $300          +$100       +50.0%  ↑ (increased)
```

**Interpretation:** Food and travel spending are up; shopping is down. Investigate food spike (dining out more?) and travel spike (vacation?).

---

## Dashboard Pages

### Page 1: Historical Observations (EDA)

**Focus:** What happened? (retrospective analysis)

**Sections:**
1. **KPI Cards** (top)
   - Latest month total spend (with MoM delta)
   - Average transaction size (with MoM delta)
   - Count of High Anomalies this month
   - Number of recurring subscriptions detected

2. **Fixed vs Variable Spend Chart** (stacked bar, 12-month history)
   - Visual breakdown of Essential vs Discretionary

3. **Category Wallet Share Chart** (donut)
   - Percentage of spend per category (last 12 months)

4. **Anomalies Table** (top 20)
   - Highest Z-score transactions, with opportunity to drill down

5. **Recurring Expenses Table** (top 10)
   - Subscriptions and habits with annual projections

### Page 2: Predictive Spend Engine

**Focus:** What will happen? (forward-looking analysis)

**Sections:**
1. **Cumulative Metric Cards** (top)
   - Predicted 3-month total spend (sum of category forecasts)
   - Budget headroom (if personal budget defined)
   - Confidence interval

2. **Forecast Slider** (interactive control)
   - User selects 1, 2, or 3 month forecast horizon
   - All charts and tables update dynamically

3. **Category Forecast Chart** (per-category line chart)
   - Historical trend (solid line from `monthly_category_spend`)
   - Forecast line (dashed, starting from last known month)
   - Confidence interval (shaded band around forecast)
   - Hover for exact values

4. **Forecast Table** (per-category projections)
   - Columns: Category, Forecast Month, Predicted Amount, Lower CI, Upper CI
   - Filtered to selected forecast horizon

5. **Total Wallet Outlook Chart** (aggregate line)
   - Summed forecast across all categories
   - Shows combined spending trajectory

---

## Key Metrics Summary

| Metric | Unit | Frequency | Use |
|--------|------|-----------|-----|
| Total Monthly Spend | $ | Monthly | Budget tracking |
| % Wallet by Category | % | Monthly | Prioritization |
| MoM Spend Change | % | Monthly | Trend detection |
| Anomaly Count | # | Monthly | Fraud & pattern detection |
| Recurring Subscriptions | # | Monthly | Commitment tracking |
| Average Transaction | $ | Monthly | Spending behavior |
| Merchant Concentration | % (top 5) | Monthly | Diversification |
| Fixed % of Budget | % | Monthly | Flexibility assessment |

---

## Continuous Improvements

### Planned Enhancements

1. **Budget Targets:** Define personal budget per category; compare actuals vs. targets
2. **Peer Benchmarking:** Compare your spending against anonymized cohorts (with privacy controls)
3. **Scenario Planning:** "What if I cut dining by 20%? How much would I save annually?"
4. **Goal Tracking:** Set savings goals and track progress (e.g., "Save $200/month for vacation")
5. **Expense Insights:** AI-generated summaries ("Your spending on coffee is 3x the median for your income bracket")

---

## Data Freshness

All analytics are computed after each run of the pipeline:

```bash
python src/pipeline/load_silver.py  # Updates silver_transactions
cd expense_tracker_dbt && dbt run     # Refreshes all Gold marts
python src/analytics/forecast_monthly_spend.py  # Updates spend_forecast
```

**Typical freshness:** 1–2 hours behind latest transactions (depending on bank statement lag and pipeline run schedule).

**Target freshness (Airflow):** Near real-time upon PDF upload to GCS.
