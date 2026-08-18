"""Retrieval diagnostics: compare modes, and check the corpus actually has the answer.

What: two tools.
  compare_modes()  - dense-only vs bm25-only vs hybrid, side by side.
  inspect_topic()  - does a section on this topic even EXIST in the index?
Inputs: a built index.
Outputs: printed reports.
Why: "retrieval is bad" has two very different causes - a ranking problem, or
     an absent document. Fixing ranking cannot surface a section the crawl never
     captured, so that question has to be answered first, from the index itself.
"""
from __future__ import annotations

import re
from collections import Counter

from .bm25 import tokenize
from .intent import extract_intents


def _row(h: dict, width: int = 62) -> str:
    title = (h["title"] or "")[:34]
    heading = (h["section_heading"] or "-")[:26]
    tag = f"{h['page_type'][:4]}/{h['segment'][:4]}/{h['language']}"
    return (f"   {h['rank']}. [{tag:<16}] conf={h['confidence']:.2f} "
            f"{title:<34} | {heading}")


def compare_modes(retriever, queries: list[str], top_k: int = 5,
                  language: str | None = None) -> None:
    """Print dense-only, BM25-only and hybrid top-K for each query."""
    for q in queries:
        print("=" * 100)
        print(f"QUERY: {q}")
        print(f"intents={extract_intents(q)}   language_filter={language}")
        print("=" * 100)
        for mode in ("dense", "bm25", "hybrid"):
            print(f"\n-- {mode.upper()}-only (raw, no metadata boosts)"
                  if mode != "hybrid"
                  else "\n-- HYBRID (weighted RRF + metadata boosts)")
            try:
                # Single-mode runs are deliberately unboosted, so this shows
                # which component is responsible for the raw ranking.
                hits = retriever.retrieve(q, top_k=top_k, language=language,
                                          mode=mode,
                                          use_metadata_boosts=(mode == "hybrid"))
            except Exception as exc:                        # noqa: BLE001
                print(f"   failed: {exc}")
                continue
            if not hits:
                print("   (no results)")
                continue
            for h in hits:
                print(_row(h))
                if h.get("boosts"):
                    print(f"        boosts: {h['boosts']}")
        print()


def inspect_topic(retriever, pattern: str, limit: int = 15,
                  field: str = "any") -> list[dict]:
    """Find indexed records whose heading/title/text matches a regex.

    This is a *lexical existence check*, independent of ranking. If it returns
    nothing, no amount of reranking will help - the content is not in the
    corpus and Phase 1 has a gap.
    """
    rx = re.compile(pattern, re.I)
    found = []
    for rec in retriever.records:
        haystack = {
            "heading": rec.get("section_heading", ""),
            "title": rec.get("title", ""),
            "text": rec.get("text", ""),
            "url": rec.get("source_url", ""),
        }
        target = " ".join(haystack.values()) if field == "any" else haystack.get(field, "")
        if rx.search(target):
            found.append(rec)
    return found[:limit]


def topic_report(retriever, topics: dict[str, str]) -> None:
    """Run several existence checks and summarise what the corpus holds."""
    print("=" * 100)
    print("CORPUS CONTENT CHECK - does the answer exist at all?")
    print("=" * 100)
    for name, pattern in topics.items():
        hits = inspect_topic(retriever, pattern, limit=200)
        by_type = Counter(h.get("page_type") for h in hits)
        by_seg = Counter(h.get("segment") for h in hits)
        by_lang = Counter(h.get("language") for h in hits)
        print(f"\n{name}")
        print(f"  pattern : {pattern}")
        print(f"  matches : {len(hits)}   page_type={dict(by_type)} "
              f"segment={dict(by_seg)} lang={dict(by_lang)}")
        if not hits:
            print("  >>> ABSENT FROM CORPUS - this is a Phase 1 gap, not a "
                  "ranking problem.")
            continue
        for h in hits[:5]:
            print(f"    - [{h.get('page_type')}/{h.get('segment')}] "
                  f"{(h.get('title') or '')[:40]:<40} | "
                  f"{(h.get('section_heading') or '-')[:30]}")
            print(f"      {h.get('citation')}")


def index_overview(retriever) -> None:
    """What is actually in the index, by type/segment/language."""
    recs = retriever.records
    print("=" * 100)
    print(f"INDEX OVERVIEW - {len(recs)} retrieval units")
    print("=" * 100)
    for field in ("document_type", "page_type", "segment", "language"):
        print(f"  {field:<14} {dict(Counter(r.get(field) for r in recs))}")
    print(f"  {'has_table':<14} {dict(Counter(bool(r.get('has_table')) for r in recs))}")
    headings = Counter((r.get("section_heading") or "").strip().lower()
                       for r in recs if r.get("section_heading"))
    print("\n  most common section headings:")
    for h, n in headings.most_common(15):
        print(f"    {n:>4}  {h[:70]}")
