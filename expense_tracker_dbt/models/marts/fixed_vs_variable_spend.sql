{{ config(materialized='table') }}

WITH monthly_summary AS (
    SELECT
        user_name,
        bank_name,
        statement_type,
        DATE_TRUNC(transaction_date, MONTH) as spend_month,
        CASE 
            WHEN category IN ('Insurance', 'Utilities', 'Rent', 'Mortgage', 'Subscription', 'Communications') THEN 'Fixed/Essential'
            WHEN category IN ('Internal', 'Payment') THEN 'Transfers'
            ELSE 'Variable/Discretionary'
        END as spend_type,
        SUM(amount) as total_amount
    FROM {{ ref('stg_transactions') }}
    GROUP BY 1, 2, 3, 4, 5
)

SELECT
    user_name,
    bank_name,
    statement_type,
    spend_month,
    spend_type,
    total_amount,
    -- Calculate percentage of month, partitioned per tenant and month
    ROUND(
        total_amount / SUM(total_amount) OVER(PARTITION BY user_name, bank_name, statement_type, spend_month), 
        4
    ) as pct_of_month
FROM monthly_summary
ORDER BY spend_month DESC, total_amount DESC
