"""
Parse raw transaction lines into structured transaction rows.

Uses bank profile regex and transaction_fields to extract ref, dates,
merchant, amount, and handle continuation lines (e.g. FX).
"""

from __future__ import annotations

import re
import logging
from typing import Any

from src.parsing.config_loader import get_bank_profile

logger = logging.getLogger(__name__)


def parse_transaction_lines(
    transaction_blocks: list[dict[str, Any]],
    bank_id: str,
    profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Parse transaction blocks into list of transaction dicts.

    Args:
        transaction_blocks: [{"page_number": N, "lines": [...]}, ...]
        bank_id: Bank identifier for profile lookup
        profile: Optional pre-loaded profile

    Returns:
        List of dicts with keys: ref, transaction_date, posted_date,
        merchant, amount, is_expense, currency, fx_amount_usd
    """
    if profile is None:
        profile = get_bank_profile(bank_id)
    if profile is None:
        logger.error("Bank profile required for parsing")
        return []

    txn_regex = re.compile(profile["transaction_line_regex"])
    continuation_patterns = profile.get("continuation_patterns", [])
    continuation_regexes = [re.compile(p) for p in continuation_patterns]
    refund_indicator = profile.get("refund_indicator", "-")
    refund_position = profile.get("refund_position", "trailing")
    currency = profile.get("default_currency", "CAD")
    fields = profile.get("transaction_fields", {})

    ref_group = fields.get("ref_group")
    trans_date_group = fields.get("transaction_date_group")
    post_date_group = fields.get("posted_date_group")
    merchant_group = fields.get("merchant_group")
    amount_group = fields.get("amount_group")

    transactions: list[dict[str, Any]] = []
    current_txn: dict[str, Any] | None = None

    for block in transaction_blocks:
        for line in block.get("lines", []):
            line = line.strip()
            if not line:
                continue

            match = txn_regex.match(line)

            if match:
                if current_txn:
                    transactions.append(current_txn)

                groups = match.groups()
                ref = str(groups[ref_group - 1]) if ref_group and ref_group <= len(groups) else ""
                trans_date = str(groups[trans_date_group - 1]) if trans_date_group and trans_date_group <= len(groups) else ""
                post_date = str(groups[post_date_group - 1]) if post_date_group and post_date_group <= len(groups) else trans_date
                merchant = str(groups[merchant_group - 1]) if merchant_group and merchant_group <= len(groups) else ""
                amount_str = str(groups[amount_group - 1]) if amount_group and amount_group <= len(groups) else "0"

                # Parse amount and is_expense from refund indicator
                amount_clean = re.sub(r"[" + re.escape(refund_indicator) + r"-]", "", amount_str).replace(",", "")
                try:
                    amount_val = float(amount_clean) if amount_clean else 0.0
                except ValueError:
                    amount_val = 0.0

                # Refund: trailing minus (Scotiabank) or CR suffix (RBC)
                is_refund = (
                    amount_str.rstrip().endswith("-")
                    or refund_indicator.upper() in amount_str.upper()
                )
                is_expense = not is_refund

                current_txn = {
                    "ref": ref,
                    "transaction_date": trans_date,
                    "posted_date": post_date,
                    "merchant": merchant.strip(),
                    "amount": amount_val,
                    "is_expense": is_expense,
                    "currency": currency,
                    "fx_amount_usd": None,
                }

            elif current_txn:
                for rx in continuation_regexes:
                    m = rx.search(line)
                    if m and m.lastindex >= 1:
                        try:
                            current_txn["fx_amount_usd"] = float(m.group(1))
                        except (ValueError, IndexError):
                            pass
                        break

    if current_txn:
        transactions.append(current_txn)

    logger.info("Parsed %d transactions", len(transactions))
    return transactions
