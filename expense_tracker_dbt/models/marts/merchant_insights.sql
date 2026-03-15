{{ config(materialized='table') }}

SELECT
    user_name,
    bank_name,
    statement_type,
    clean_merchant,
    category,
    SUM(amount) AS total_spent,
    COUNT(*) AS visit_count,
    -- Percentage of total wallet per user/bank/type
    ROUND(
        SUM(amount) / SUM(SUM(amount)) OVER(PARTITION BY user_name, bank_name, statement_type), 
        4
    ) * 100 AS percent_of_total_wallet
FROM {{ ref('stg_transactions') }}
WHERE transaction_scope = 'External'
GROUP BY 1, 2, 3, 4, 5
ORDER BY total_spent DESC
