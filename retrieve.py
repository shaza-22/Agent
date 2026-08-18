#!/usr/bin/env python3
"""Query the retrieval index from the command line.

    python retrieve.py "What documents do I need to open an account?"
    python retrieve.py "رسوم بطاقات بنك مصر" --language ar
    python retrieve.py "compare credit cards" --top-k 8 --mode bm25

The library API is `from retrieval import retrieve` - this is a thin wrapper
so the same code path serves both.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap

from retrieval import Retriever


def render(results: list[dict], width: int = 96, snippet_chars: int = 320) -> str:
    if not results:
        return ("No results.\n"
                "  - the query may share no terms with the corpus, or\n"
                "  - a --language filter may have excluded everything.")
    out = []
    for r in results:
        head = (f"#{r['rank']}  score={r['score']:.4f}  "
                f"[{r['language']}/{r['document_type']}]")
        if r.get("dense_score") is not None or r.get("bm25_score") is not None:
            head += (f"  (dense={r['dense_score']}, bm25={r['bm25_score']})")
        out.append(head)
        out.append(f"    title   : {r['title']}")
        out.append(f"    section : {r['section_heading'] or '(none)'}")
        if r.get("pdf_page"):
            out.append(f"    page    : ~{r['pdf_page']} (estimated)")
        out.append(f"    cite    : {r['citation']}")
        snippet = " ".join(r["text"].split())[:snippet_chars]
        out.append(textwrap.fill(snippet, width=width,
                                 initial_indent="    text    : ",
                                 subsequent_indent="              "))
        out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Search the Banque Misr corpus")
    ap.add_argument("query", nargs="+")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--language", choices=["en", "ar"], default=None)
    ap.add_argument("--document-type", choices=["html", "pdf"], default=None)
    ap.add_argument("--mode", choices=["hybrid", "dense", "bm25"], default="hybrid")
    ap.add_argument("--max-per-document", type=int, default=2)
    ap.add_argument("--index-dir", default="data/retrieval")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    query = " ".join(args.query)
    try:
        retriever = Retriever(args.index_dir)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc))

    results = retriever.retrieve(
        query, top_k=args.top_k, language=args.language,
        document_type=args.document_type, mode=args.mode,
        max_per_document=args.max_per_document)

    if args.json:
        json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return

    print(f"query : {query}")
    print(f"model : {retriever.model_name}   mode: {args.mode}"
          + (f"   language={args.language}" if args.language else ""))
    print(f"units : {len(retriever.records)} indexed\n")
    print(render(results))


if __name__ == "__main__":
    main()
