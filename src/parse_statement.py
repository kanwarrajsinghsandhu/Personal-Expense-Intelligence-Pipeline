"""
Orchestrate bank statement parsing: extract → parse → clean.

Run from project root: python -m src.parse_statement [pdf_path] [--bank rbc|scotia]
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from src.parsing.extract_pdf import extract_pdf
from src.parsing.parse_transactions import parse_transaction_lines
from src.parsing.clean_transactions import clean_transactions
from src.parsing.config_loader import get_bank_profile

logger = logging.getLogger(__name__)


def parse_statement(
    pdf_path: str | Path,
    bank_id: str | None = None,
    enrich: bool = False,
    models_dir: str | Path | None = None,
    catalog_path: str | Path | None = None,
    use_llm_fallback: bool = False,
    llm_provider: str = "groq",
) -> tuple[pd.DataFrame, str | None]:
    """
    Parse a bank statement PDF into a cleaned transactions DataFrame.

    Args:
        pdf_path: Path to PDF file
        bank_id: Optional bank_id; auto-detected if None
        enrich: If True, run ML classification and catalog overlay (category, subcategory, merchant_standardized)
        models_dir: Path to models/ for enrichment (default: project models/)
        catalog_path: Path to merchant_catalog.json (default: data/merchant_catalog.json)
        use_llm_fallback: If True, use LLM (Groq/Gemini/OpenAI) for merchants not matched by catalog.
        llm_provider: LLM provider to use - 'groq' (free), 'gemini', or 'openai'.

    Returns:
        (transactions_df, bank_id or None on failure)
    """
    pages_text, transaction_blocks, detected_bank = extract_pdf(pdf_path, bank_id)
    if detected_bank is None:
        return pd.DataFrame(), None

    profile = get_bank_profile(detected_bank)
    if profile is None:
        return pd.DataFrame(), None

    transactions = parse_transaction_lines(transaction_blocks, detected_bank, profile)
    df = clean_transactions(transactions, pages_text, detected_bank, profile)

    if enrich and not df.empty:
        from src.enrich import enrich_transactions
        df = enrich_transactions(
            df,
            models_dir=models_dir,
            catalog_path=catalog_path,
            use_llm_fallback=use_llm_fallback,
            llm_provider=llm_provider,
        )

    return df, detected_bank



def main() -> None:
    parser = argparse.ArgumentParser(description="Parse bank statement PDF")
    parser.add_argument("pdf_path", nargs="?", default="data/raw/October 2025 e-statement.pdf")
    parser.add_argument("--bank", choices=["rbc", "scotia"], help="Force bank format")
    parser.add_argument("--output", "-o", help="Output CSV path")
    parser.add_argument("--enrich", action="store_true", help="Run ML + catalog enrichment (category, subcategory, merchant_standardized)")
    args = parser.parse_args()

    df, bank_id = parse_statement(args.pdf_path, args.bank, enrich=args.enrich)
    if bank_id is None:
        logger.error("Parsing failed")
        return

    print(f"Parsed {len(df)} transactions (bank: {bank_id})")
    print(df.head())

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_path, index=False)
        print(f"Saved to {out_path}")


if __name__ == "__main__":
    main()
