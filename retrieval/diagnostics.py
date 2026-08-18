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


def explain_query(retriever, query: str, depth: int = 20,
                  language: str | None = None) -> None:
    """Full scoring breakdown for the top-N candidates of one query.

    Every component that decides the ranking, side by side: dense cosine, BM25,
    fused RRF, each metadata boost, heading match, page type, segment,
    language. This is the evidence needed to tell a ranking bug from a corpus
    gap - which of the two you have determines whether tuning can help at all.
    """
    from .intent import extract_intents, extract_topics, heading_matches_intent
    intents = extract_intents(query)
    topics = extract_topics(query)

    print("=" * 118)
    print(f"EXPLAIN: {query}")
    print(f"intents={intents}  topics={topics}  language_filter={language}")
    print("=" * 118)

    hits = retriever.retrieve(query, top_k=depth, language=language,
                              max_per_document=99, auto_filter=False)
    if not hits:
        print("  (no candidates)")
        return

    print(f"{'#':>3} {'dense':>6} {'bm25':>6} {'base':>8} {'final':>8} {'conf':>5} "
          f"{'hd':>3} {'page':<8} {'segment':<9} {'lg':<3} title | heading")
    print("-" * 118)
    for h in hits:
        hd = "Y" if heading_matches_intent(h["section_heading"], intents) else "-"
        dense = f"{h['dense_score']:.3f}" if h["dense_score"] is not None else "  -  "
        bm25 = f"{h['bm25_score']:.2f}" if h["bm25_score"] is not None else "  -  "
        title = (h["title"] or "")[:30]
        heading = (h["section_heading"] or "-")[:24]
        print(f"{h['rank']:>3} {dense:>6} {bm25:>6} {h['base_score']:>8.5f} "
              f"{h['score']:>8.5f} {h['confidence']:>5.2f} {hd:>3} "
              f"{h['page_type']:<8} {h['segment']:<9} {h['language']:<3} "
              f"{title} | {heading}")
        if h["boosts"]:
            print(f"      boosts: {h['boosts']}")
    print()


def show_records(retriever, pattern: str, limit: int = 10,
                 chars: int = 400) -> None:
    """Print the full text of records matching a pattern.

    Use this to answer "does this section actually ANSWER the question?"
    A count of matches says a term appears; only reading them says whether the
    corpus contains the answer at all.
    """
    hits = inspect_topic(retriever, pattern, limit=limit)
    print("=" * 100)
    print(f"RECORDS MATCHING: {pattern}   ({len(hits)} shown)")
    print("=" * 100)
    for i, rec in enumerate(hits, 1):
        print(f"\n[{i}] {rec.get('title')}  |  {rec.get('section_heading') or '-'}")
        print(f"    {rec.get('citation')}")
        print(f"    type={rec.get('document_type')} page={rec.get('page_type')} "
              f"segment={rec.get('segment')} lang={rec.get('language')} "
              f"table={rec.get('has_table')} words={rec.get('word_count')}")
        text = " ".join((rec.get("text") or "").split())
        print(f"    {text[:chars]}")


def duplicate_report(retriever, limit: int = 15) -> None:
    """Records whose text is identical - surviving Sitecore URL variants."""
    from collections import defaultdict
    groups = defaultdict(list)
    for rec in retriever.records:
        groups[(rec.get("text") or "")[:300]].append(rec)
    dupes = sorted(((k, v) for k, v in groups.items() if len(v) > 1),
                   key=lambda kv: -len(kv[1]))
    print("=" * 100)
    print(f"DUPLICATE TEXT CLUSTERS: {len(dupes)} groups, "
          f"{sum(len(v) for _, v in dupes)} records involved")
    print("=" * 100)
    for _, recs in dupes[:limit]:
        print(f"\n  {len(recs)}x  {(recs[0].get('title') or '')[:50]} | "
              f"{(recs[0].get('section_heading') or '-')[:30]}")
        for r in recs[:4]:
            print(f"      {r.get('source_url')}")


def health_check(retriever) -> int:
    """Flag metadata distributions that indicate a classifier bug.

    Runs against the real index, so it catches problems no offline fixture can:
    a classifier that labels half the corpus corporate, or a language tagger
    that gives up on a whole document type. Returns the number of warnings.
    """
    from collections import Counter
    recs = retriever.records
    n = len(recs) or 1
    warn = 0

    def flag(msg):
        nonlocal warn
        warn += 1
        print(f"  WARN  {msg}")

    print("=" * 100)
    print(f"INDEX HEALTH - {n} retrieval units")
    print("=" * 100)

    seg = Counter(r.get("segment") for r in recs)
    print(f"  segment    {dict(seg)}")
    corp, cons = seg.get("corporate", 0), seg.get("consumer", 0)
    if corp > n * 0.4 and corp > cons * 3:
        flag(f"{corp} corporate vs {cons} consumer - the segment classifier is "
             f"probably over-triggering. Every corporate label costs a ranking "
             f"penalty on personal questions.")

    lang = Counter(r.get("language") for r in recs)
    print(f"  language   {dict(lang)}")
    if lang.get("unknown", 0) > n * 0.1:
        flag(f"{lang['unknown']} units have unknown language ({lang['unknown']/n:.0%})")
    pdf_unknown = sum(1 for r in recs if r.get("document_type") == "pdf"
                      and r.get("language") == "unknown")
    if pdf_unknown:
        flag(f"{pdf_unknown} PDF units still unlabelled - language should be "
             f"derived from chunk text at index time")

    page = Counter(r.get("page_type") for r in recs)
    print(f"  page_type  {dict(page)}")
    if page.get("other", 0) > n * 0.3:
        flag(f"{page['other']} units classified 'other' - URL patterns may not "
             f"match this site's structure")

    tables = sum(1 for r in recs if r.get("has_table"))
    print(f"  has_table  {tables}")
    if not tables:
        flag("no section carries a table - fee/rate answers may be unreachable")

    dupes = len(recs) - len({(r.get("text") or "")[:300] for r in recs})
    print(f"  duplicate-text units  {dupes}")
    if dupes > n * 0.1:
        flag(f"{dupes} units duplicate another's text - surviving URL variants")

    print(f"\n  {warn} warning(s)")
    return warn
