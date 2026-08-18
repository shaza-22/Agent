#!/usr/bin/env python3
"""Phase 2 retrieval diagnostics.

    python diagnose_retrieval.py --overview
    python diagnose_retrieval.py --content-check
    python diagnose_retrieval.py --compare          # the 5 real test queries
    python diagnose_retrieval.py --compare --query "your question here"
"""
from __future__ import annotations

import argparse

from retrieval import Retriever
from retrieval.diagnostics import (compare_modes, index_overview, topic_report)

TEST_QUERIES = [
    "What are the eligibility requirements for a Banque Misr credit card?",
    "What documents do I need to open an account at Banque Misr?",
    "What is the annual fee on Banque Misr cards?",
    "ما هي رسوم بطاقات بنك مصر؟",
    "Compare Banque Misr credit cards",
]

# Existence checks for the four topics the test queries depend on.
CONTENT_CHECKS = {
    "credit-card eligibility": r"(eligib|criteria|who can apply|minimum (monthly )?"
                               r"(income|salary)|شروط|الاستحقاق)",
    "account-opening documents": r"(required documents|documents required|"
                                 r"documents needed|to open an account|"
                                 r"المستندات|الاوراق المطلوبة)",
    "card fees (annual)": r"(annual fee|yearly fee|annual subscription|"
                          r"رسوم سنوية|الرسوم السنوية)",
    "card fees (any)": r"(fee|charge|commission|tariff|رسوم|عمولة)",
    "individual/consumer credit cards": r"(credit card|بطاقة ائتمان)",
    "issuance vs annual fee": r"(issuance fee|issue fee|replacement fee|رسوم الاصدار)",
}


def main() -> None:
    ap = argparse.ArgumentParser(description="Diagnose retrieval quality")
    ap.add_argument("--index-dir", default="data/retrieval")
    ap.add_argument("--overview", action="store_true")
    ap.add_argument("--content-check", action="store_true")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--query", action="append", default=None)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--language", choices=["en", "ar"], default=None)
    args = ap.parse_args()

    r = Retriever(args.index_dir)
    if not (args.overview or args.content_check or args.compare):
        args.overview = args.content_check = args.compare = True

    if args.overview:
        index_overview(r)
        print()
    if args.content_check:
        topic_report(r, CONTENT_CHECKS)
        print()
    if args.compare:
        queries = args.query or TEST_QUERIES
        for q in queries:
            # The Arabic query is run with its language filter, as you ran it.
            lang = args.language or ("ar" if any("؀" <= c <= "ۿ" for c in q)
                                     else None)
            compare_modes(r, [q], top_k=args.top_k, language=lang)


if __name__ == "__main__":
    main()
