{{ config(materialized='table') }}

SELECT
    user_name,
    bank_name,
    statement_type,
    DATE_TRUNC(transaction_date, MONTH) AS spend_month,
    category,
    SUM(amount) AS total_amount,
    COUNT(*) AS transaction_count,
    ROUND(AVG(amount), 2) AS avg_transaction_size
FROM {{ ref('stg_transactions') }}
WHERE transaction_scope = 'External'
GROUP BY 1, 2, 3, 4, 5
ORDER BY spend_month DESC, total_amount DESC
