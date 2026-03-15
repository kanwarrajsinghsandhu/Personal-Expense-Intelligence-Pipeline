"""
Parsing module for Expense Tracker.

Extracts and parses bank statement PDFs (RBC, Scotiabank) into structured
transaction data for classification and analysis.
"""

from src.parsing.config_loader import get_bank_profile, list_bank_profiles
from src.parsing.bank_detector import detect_bank
from src.parsing.extract_pdf import extract_pdf
from src.parsing.parse_transactions import parse_transaction_lines
from src.parsing.clean_transactions import clean_transactions

__all__ = [
    "get_bank_profile",
    "list_bank_profiles",
    "detect_bank",
    "extract_pdf",
    "parse_transaction_lines",
    "clean_transactions",
]
