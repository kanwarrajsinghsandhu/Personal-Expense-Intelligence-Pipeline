{{ config(materialized='table') }}

WITH date_diffs AS (
    SELECT
        user_name,
        bank_name,
        statement_type,
        clean_merchant,
        amount,
        transaction_date,
        -- Corrected: Partition by tenant dimensions to avoid cross-user matching
        DATE_DIFF(transaction_date, 
                  LAG(transaction_date) OVER (
                      PARTITION BY user_name, bank_name, statement_type, clean_merchant, CAST(amount AS NUMERIC) 
                      ORDER BY transaction_date
                  ), 
                  DAY) as days_since_last
    FROM {{ ref('stg_transactions') }}
    WHERE transaction_scope = 'External'
)

SELECT
    user_name,
    bank_name,
    statement_type,
    clean_merchant,
    amount,
    COUNT(*) as occurrence_count,
    CASE 
        WHEN AVG(days_since_last) BETWEEN 25 AND 35 THEN 'Monthly Subscription'
        WHEN AVG(days_since_last) BETWEEN 6 AND 8 THEN 'Weekly Habit'
        WHEN AVG(days_since_last) BETWEEN 80 AND 100 THEN 'Quarterly'
        ELSE 'Irregular'
    END as recurrence_type,
    amount * 12 as projected_annual_cost
FROM date_diffs
GROUP BY 1, 2, 3, 4, 5
HAVING COUNT(*) >= 2
ORDER BY projected_annual_cost DESC
