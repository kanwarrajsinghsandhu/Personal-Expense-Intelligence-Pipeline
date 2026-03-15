{{ config(materialized='table') }}

WITH monthly_spend AS (
    SELECT * FROM {{ ref('monthly_category_spend') }}
)

SELECT
    user_name,
    bank_name,
    statement_type,
    spend_month,
    category,
    total_amount,
    -- Calculate change from previous month, partitioned by tenant dimensions
    LAG(total_amount) OVER (
        PARTITION BY user_name, bank_name, statement_type, category 
        ORDER BY spend_month
    ) AS prev_month_amount,
    total_amount - LAG(total_amount) OVER (
        PARTITION BY user_name, bank_name, statement_type, category 
        ORDER BY spend_month
    ) AS net_change
FROM monthly_spend
