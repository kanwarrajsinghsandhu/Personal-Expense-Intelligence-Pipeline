{{ config(materialized='view') }}

WITH raw_silver AS (
    SELECT * FROM {{ source('credit_card_analytics', 'silver_transactions') }}
)

SELECT
    dedup_key,
    user_name,
    bank_name,
    statement_type,
    date AS transaction_date,
    description AS raw_description,
    amount,
    -- If merchant_standardized is null, we fall back to the raw description
    COALESCE(merchant_standardized, description) AS clean_merchant,
    category,
    subcategory,
    CASE 
        WHEN is_internal = TRUE THEN 'Internal'
        ELSE 'External'
    END AS transaction_scope
FROM raw_silver
