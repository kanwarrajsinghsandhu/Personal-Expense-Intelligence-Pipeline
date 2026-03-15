{{ config(materialized='table') }}

WITH category_stats AS (
    SELECT 
        user_name,
        bank_name,
        statement_type,
        category,
        AVG(amount) as avg_spend,
        STDDEV(amount) as stddev_spend
    FROM {{ ref('stg_transactions') }}
    WHERE transaction_scope = 'External'
    GROUP BY 1, 2, 3, 4
)

SELECT
    t.user_name,
    t.bank_name,
    t.statement_type,
    t.transaction_date,
    t.clean_merchant,
    t.category,
    t.amount,
    s.avg_spend,
    -- Flag if amount is > 2 standard deviations from the mean (Personalized per user/category)
    CASE 
        WHEN t.amount > (s.avg_spend + (2 * COALESCE(s.stddev_spend, 0))) THEN 'High Anomaly'
        WHEN t.amount > (s.avg_spend + COALESCE(s.stddev_spend, 0)) THEN 'Moderate Anomaly'
        ELSE 'Normal'
    END as anomaly_flag
FROM {{ ref('stg_transactions') }} t
JOIN category_stats s 
    ON t.category = s.category 
    AND t.user_name = s.user_name
    AND t.bank_name = s.bank_name
    AND t.statement_type = s.statement_type
WHERE t.transaction_scope = 'External'
ORDER BY t.amount DESC