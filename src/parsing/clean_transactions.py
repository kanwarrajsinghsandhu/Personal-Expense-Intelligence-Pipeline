"""
Clean and enrich parsed transactions.

Adds statement period, datetime parsing, transaction_type, is_foreign_currency,
and is_internal flag.
"""

from __future__ import annotations

import re
import logging
from typing import Any

import numpy as np
import pandas as pd

from src.parsing.config_loader import get_bank_profile
from pandas.api.types import is_datetime64_any_dtype


logger = logging.getLogger(__name__)


def _extract_statement_period(
    pages_text: dict[int, str],
    profile: dict[str, Any],
) -> tuple[pd.Timestamp | None, pd.Timestamp | None, int | None]:
    """Extract statement_start, statement_end, statement_year from first page."""
    meta = profile.get("statement_metadata", {})
    search_page = meta.get("search_page", 1)
    text = pages_text.get(search_page, "")
    clean_text = re.sub(r"\s+", " ", text.replace("\\", "")).strip()

    period_pattern = meta.get("period_pattern")
    date_pattern = meta.get("date_pattern")
    period_format = meta.get("period_format", "%b %d, %Y")

    statement_start: pd.Timestamp | None = None
    statement_end: pd.Timestamp | None = None
    statement_year: int | None = None

    if period_pattern:
        m = re.search(period_pattern, clean_text, re.IGNORECASE)
        if m and m.lastindex >= 2:
            try:
                start_str = m.group(1).strip()
                end_str = m.group(2).strip()
                
                # End date usually has the year in both RBC and Scotia
                statement_end = pd.to_datetime(end_str, format=period_format)
                statement_year = statement_end.year
                
                # Start date might lack the year (e.g. RBC: "NOV 28")
                if "," not in start_str:
                    statement_start = pd.to_datetime(f"{start_str}, {statement_year}", format="%b %d, %Y")
                    # Handle Dec -> Jan rollover across calendar years
                    if statement_start > statement_end:
                        statement_start = pd.to_datetime(f"{start_str}, {statement_year - 1}", format="%b %d, %Y")
                else:
                    statement_start = pd.to_datetime(start_str, format=period_format)

            except (ValueError, TypeError):
                pass

    if statement_end is None and date_pattern:
        m = re.search(date_pattern, clean_text, re.IGNORECASE)
        if m and m.lastindex >= 1:
            try:
                statement_end = pd.to_datetime(m.group(1).strip(), format=period_format)
                statement_year = statement_end.year
            except (ValueError, TypeError):
                pass

    return statement_start, statement_end, statement_year


def _parse_date_with_year(
    date_str: str,
    statement_year: int,
    statement_start: pd.Timestamp | None,
    statement_end: pd.Timestamp | None,
    date_format: str,
) -> pd.Timestamp | None:
    """Parse date string with year inference for month/day rollover."""
    date_str = date_str.replace(",", "").strip()
    full_str = f"{date_str}, {statement_year}"
    format_with_year = "%b %d, %Y" if "%b" in date_format else "%Y-%m-%d"
    try:
        parsed = pd.to_datetime(full_str, format=format_with_year)
    except (ValueError, TypeError):
        return None
    # Handle year rollover (e.g. Dec 31 -> Jan 1)
    if statement_start is not None and parsed < statement_start:
        try:
            parsed = pd.to_datetime(f"{date_str}, {statement_year + 1}", format=format_with_year)
        except (ValueError, TypeError):
            pass
    if statement_end is not None and parsed > statement_end:
        try:
            parsed = pd.to_datetime(f"{date_str}, {statement_year - 1}", format=format_with_year)
        except (ValueError, TypeError):
            pass
    return parsed


def _is_internal_transaction(merchant: str) -> bool:
    """Check if merchant indicates internal transfer/payment."""
    keywords = [
        "PAYMENT FROM",
        "PAYMENT TO",
        "TRANSFER FROM",
        "TRANSFER TO",
        "CREDIT CARD",
        "DEPOSIT",
        "WITHDRAWAL",
        "PAYMENT - THANK YOU",
        "PAIEMENT - MERCI",
        "AUTOMATIC PAYMENT",
        "MISC PAYMENT",
    ]
    return any(k in (merchant or "").upper() for k in keywords)


def clean_transactions(
    transactions: list[dict[str, Any]],
    pages_text: dict[int, str],
    bank_id: str,
    profile: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """
    Clean and enrich parsed transactions into a DataFrame.

    Adds: statement_start_date, statement_end_date, statement_year,
    transaction_date_dt, posted_date_dt, transaction_type, is_foreign_currency,
    is_internal, and normalizes amount sign.
    """
    if profile is None:
        profile = get_bank_profile(bank_id)
    if profile is None:
        logger.error("Bank profile required for cleaning")
        return pd.DataFrame()

    df = pd.DataFrame(transactions)
    if df.empty:
        return df

    # Amount sign: expense = positive, refund/payment = negative
    df["amount"] = df.apply(
        lambda r: r["amount"] if r["is_expense"] else -r["amount"],
        axis=1,
    )

    # Transaction type
    conditions = [
        df["is_expense"] == True,
        (~df["is_expense"]) & df["merchant"].str.contains("PAYMENT", case=False, na=False),
        ~df["is_expense"],
    ]
    choices = ["Expense", "Payment", "Refund"]
    df["transaction_type"] = np.select(conditions, choices, default="Unknown")

    # is_foreign_currency
    df["is_foreign_currency"] = df["fx_amount_usd"].notna()

    # Statement period
    start_dt, end_dt, year = _extract_statement_period(pages_text, profile)
    df["statement_start_date"] = start_dt
    df["statement_end_date"] = end_dt
    df["statement_year"] = year

    # Parse dates with year
    date_format = profile.get("date_format", "%b %d")
    if year is not None:
        df["transaction_date_dt"] = df["transaction_date"].apply(
            lambda s: _parse_date_with_year(
                str(s), year, start_dt, end_dt, date_format
            )
        )
        df["posted_date_dt"] = df["posted_date"].apply(
            lambda s: _parse_date_with_year(
                str(s), year, start_dt, end_dt, date_format
            )
        )
    else:
        df["transaction_date_dt"] = pd.NaT
        df["posted_date_dt"] = pd.NaT

    # is_internal
    df["is_internal"] = df["merchant"].apply(_is_internal_transaction)
    df.loc[df["is_internal"], "transaction_type"] = "Internal Transaction"
    
    

    # Feature engineering for exploration and downstream use
    df = _add_statement_features(df)

    logger.info("Cleaned %d transactions", len(df))
    return df



def _add_statement_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add statement_id, statement_year_month, posting_lag_days, transaction_cycle_day, transaction_weekday, is_weekend."""
    if df.empty:
        return df

    # Ensure statement_start_date / statement_end_date are datetime-like
    for col in ["statement_start_date", "statement_end_date"]:
        if col in df.columns and not is_datetime64_any_dtype(df[col]):
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # statement_id: durable id from period
    has_dates = df["statement_start_date"].notna() & df["statement_end_date"].notna()
    df["statement_id"] = np.where(
        has_dates,
        df["statement_start_date"].dt.date.astype(str) + "__" + df["statement_end_date"].dt.date.astype(str),
        pd.NA,
    )

    # statement_year_month for partitioning
    df["statement_year_month"] = df["statement_end_date"].dt.strftime("%Y%m")

    # posting_lag_days
    df["posting_lag_days"] = (df["posted_date_dt"] - df["transaction_date_dt"]).dt.days.astype("Int64")

    # transaction_cycle_day (1-based day within statement)
    df["transaction_cycle_day"] = (
        (df["transaction_date_dt"] - df["statement_start_date"]).dt.days + 1
    ).astype("Int64")

    # transaction_weekday, is_weekend
    df["transaction_weekday"] = df["transaction_date_dt"].dt.day_name()
    df["is_weekend"] = df["transaction_date_dt"].dt.dayofweek.isin([5, 6])

    return df