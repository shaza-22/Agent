"""The retrieval API. Importable by Phase 3; the CLI is a thin wrapper.

What: `Retriever.retrieve(query, top_k, language)` returning citable results.
Inputs: a persisted index directory.
Outputs: list of result dicts, each carrying full provenance.
Why: Phase 3's agent needs one stable call that returns evidence it can cite.
     Everything below - fusion, filtering, diversity - exists to make the top
     few results actually worth reading, because an agent only ever sees a
     handful.

Retrieval is HYBRID by default: dense vectors for meaning, BM25 for exact
terms, fused with Reciprocal Rank Fusion. Neither alone is adequate here.
Dense search misses "EGP 350" because numbers carry little semantic signal;
BM25 misses "what do I need to open an account" -> "required documents"
because they share no words. RRF is used rather than score-weighting because
cosine and BM25 scores are not on comparable scales.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .bm25 import BM25, tokenize
from .embedders import Embedder, build_embedder
from .index import VectorIndex
from .intent import (extract_intents, extract_topics, heading_matches_intent,
                     query_segment)

DEFAULT_INDEX_DIR = Path("data/retrieval")
RRF_K = 60          # standard RRF damping constant

# RRF scores are tiny and non-linear, so metadata boosts are expressed in units
# of "how far apart are rank 1 and rank 11 in one ranking". A boost of 1.0 unit
# therefore moves a result about ten places. Stated this way the constants below
# are interpretable instead of arbitrary decimals.
BOOST_UNIT = 1.0 / (RRF_K + 1) - 1.0 / (RRF_K + 11)

# Dense is weighted above lexical. BM25 is essential for exact terms ("EGP 350")
# but on this corpus it over-promotes short news items that name every product,
# and plain RRF ignores score magnitude entirely - a useless BM25 top-1
# contributes exactly as much as an excellent dense top-1.
DENSE_WEIGHT = 1.0
BM25_WEIGHT = 0.55

BOOSTS = {
    "heading_match": 2.0,      # section heading is literally what was asked for
    "page_product": 0.6,
    "page_news": -2.5,         # the dominant failure mode on product questions
    "page_about": -1.2,
    "segment_mismatch": -1.5,  # corporate page answering a personal question
    "has_table_fee_query": 0.8,
    "pdf_fee_query": 0.5,
}


class Retriever:
    def __init__(self, index_dir: Path | str = DEFAULT_INDEX_DIR,
                 embedder: Embedder | None = None, enable_bm25: bool = True):
        self.index = VectorIndex.load(Path(index_dir))
        self.records = self.index.records
        self._embedder = embedder
        self._bm25 = None
        if enable_bm25 and self.records:
            self._bm25 = BM25([tokenize(r["text"]) for r in self.records])

    @property
    def embedder(self) -> Embedder:
        """Loaded lazily: BM25-only retrieval must not require the model."""
        if self._embedder is None:
            cfg = self.index.manifest.get("embedder", {})
            name = cfg.get("model_name", "")
            if cfg.get("backend") == "hashing":
                name = "hashing"
            self._embedder = build_embedder(name)
        return self._embedder

    @property
    def model_name(self) -> str:
        return self.index.manifest.get("embedder", {}).get("model_name", "unknown")

    # -- ranking -------------------------------------------------------------
    def _dense(self, query: str, depth: int) -> list[tuple[int, float]]:
        try:
            qv = self.embedder.encode_queries([query])[0]
        except SystemExit:
            return []       # model unavailable -> BM25 alone, not a crash
        return self.index.search(qv, depth)

    def _fuse(self, dense: list[tuple[int, float]],
              lexical: list[tuple[int, float]],
              dense_weight: float = DENSE_WEIGHT,
              bm25_weight: float = BM25_WEIGHT) -> dict[int, dict]:
        """Weighted Reciprocal Rank Fusion over two incomparable rankings."""
        fused: dict[int, dict] = {}

        def slot(i):
            return fused.setdefault(i, {"dense_score": None, "bm25_score": None,
                                        "rrf": 0.0})

        for rank, (idx, score) in enumerate(dense):
            s = slot(idx)
            s["dense_score"] = score
            s["rrf"] += dense_weight / (RRF_K + rank + 1)
        for rank, (idx, score) in enumerate(lexical):
            s = slot(idx)
            s["bm25_score"] = score
            s["rrf"] += bm25_weight / (RRF_K + rank + 1)
        return fused

    @staticmethod
    def _metadata_boost(rec: dict, intents: list[str], topics: list[str],
                        want_segment: str) -> tuple[float, dict]:
        """Small, explainable adjustments from what we know about the record.

        Returned alongside the score so a ranking can always be justified -
        an agent citing a result should be able to say why it surfaced.
        """
        applied: dict[str, float] = {}
        total = 0.0
        product_question = bool(intents or topics)

        if intents and heading_matches_intent(rec.get("section_heading", ""), intents):
            applied["heading_match"] = BOOSTS["heading_match"]

        page_type = rec.get("page_type", "other")
        if product_question:
            if page_type == "news":
                applied["page_news"] = BOOSTS["page_news"]
            elif page_type == "about":
                applied["page_about"] = BOOSTS["page_about"]
            elif page_type == "product":
                applied["page_product"] = BOOSTS["page_product"]

        seg = rec.get("segment", "unknown")
        if want_segment in ("consumer", "consumer_default") and seg == "corporate":
            applied["segment_mismatch"] = BOOSTS["segment_mismatch"]
        elif want_segment == "corporate" and seg == "consumer":
            applied["segment_mismatch"] = BOOSTS["segment_mismatch"]

        if {"fees", "rates"} & set(intents):
            if rec.get("has_table"):
                applied["has_table_fee_query"] = BOOSTS["has_table_fee_query"]
            if rec.get("document_type") == "pdf":
                # Tariff schedules are published as attachments.
                applied["pdf_fee_query"] = BOOSTS["pdf_fee_query"]

        total = sum(applied.values()) * BOOST_UNIT
        return total, applied

    @staticmethod
    def _confidence(dense_score, bm25_score, boosts: dict) -> float:
        """Rough 0-1 evidence estimate, for the "no good result" decision.

        Cosine similarity is the only calibrated number available, so it does
        most of the work; a lexical hit and a matching heading are corroborating
        evidence. The absolute value is NOT comparable across embedding models -
        calibrate the threshold on your own corpus (see --explain).
        """
        conf = 0.0
        if dense_score is not None:
            conf += 0.65 * max(0.0, min(1.0, float(dense_score)))
        if bm25_score:
            conf += 0.20 * min(1.0, float(bm25_score) / 8.0)
        if "heading_match" in boosts:
            conf += 0.15
        return round(min(1.0, conf), 4)

    def retrieve(self, query: str, top_k: int = 5, language: str | None = None,
                 document_type: str | None = None, max_per_document: int = 2,
                 mode: str = "hybrid", depth: int | None = None,
                 use_metadata_boosts: bool | None = None, min_confidence: float = 0.0,
                 page_type: str | None = None, auto_filter: bool = True,
                 exclude_page_types: tuple[str, ...] = (),
                 dense_weight: float = DENSE_WEIGHT,
                 bm25_weight: float = BM25_WEIGHT) -> list[dict]:
        """Return up to `top_k` citable results, best first.

        language:          'en' | 'ar' | None (no filter)
        document_type:     'html' | 'pdf' | None
        max_per_document:  cap results from any single source URL. Broad queries
                           ("compare the credit cards") otherwise return five
                           sections of one page instead of five products.
        mode:              'hybrid' | 'dense' | 'bm25'
        """
        if not query.strip() or not self.records:
            return []

        # A comparison question wants breadth: several products, one strong
        # section each, rather than three sections of the same card.
        if max_per_document > 1 and "compare" in extract_intents(query):
            max_per_document = 1

        # Over-fetch, because filtering and the per-document cap both discard.
        depth = depth or max(50, top_k * 10)

        dense = self._dense(query, depth) if mode in ("hybrid", "dense") else []
        lexical = (self._bm25.search(query, depth)
                   if (self._bm25 and mode in ("hybrid", "bm25")) else [])

        if mode == "dense":
            ranked = [(i, {"dense_score": s, "bm25_score": None, "rrf": s})
                      for i, s in dense]
        elif mode == "bm25":
            ranked = [(i, {"dense_score": None, "bm25_score": s, "rrf": s})
                      for i, s in lexical]
        else:
            ranked = list(self._fuse(dense, lexical, dense_weight,
                                     bm25_weight).items())

        intents = extract_intents(query)
        topics = extract_topics(query)
        want_segment = query_segment(query)

        # Metadata boosts are calibrated in RRF units. Raw cosine (dense) and
        # raw BM25 scores live on entirely different scales, where those
        # constants would be either negligible or overwhelming - so single-mode
        # retrieval stays pure unless boosts are asked for explicitly.
        if mode != "hybrid" and use_metadata_boosts is None:
            use_metadata_boosts = False
        if use_metadata_boosts is None:
            use_metadata_boosts = True

        if use_metadata_boosts:
            adjusted = []
            for idx, sc in ranked:
                if idx >= len(self.records):
                    continue
                boost, applied = self._metadata_boost(
                    self.records[idx], intents, topics, want_segment)
                sc = dict(sc)
                sc["base_rrf"] = sc["rrf"]
                sc["rrf"] = sc["rrf"] + boost
                sc["boosts"] = applied
                adjusted.append((idx, sc))
            ranked = adjusted
        else:
            ranked = [(i, {**sc, "base_rrf": sc["rrf"], "boosts": {}})
                      for i, sc in ranked if i < len(self.records)]

        ranked.sort(key=lambda kv: -kv[1]["rrf"])

        # Metadata FILTERING, not just boosting. A boost can be outvoted by a
        # strong base score, and on a product question a news article is not a
        # slightly worse answer - it is the wrong kind of document. So when the
        # query clearly asks about a product AND enough product-type results
        # exist to satisfy it, drop news/about outright. The guard matters: if
        # the corpus cannot fill top_k with product pages we keep them, because
        # a weak answer beats an empty one.
        drop = set(exclude_page_types)
        if auto_filter and (intents or topics):
            candidates = [self.records[i] for i, _ in ranked[:depth]]
            n_product = sum(1 for r in candidates
                            if r.get("page_type") == "product"
                            and (not language or r.get("language") == language))
            if n_product >= top_k:
                drop |= {"news", "about"}
        if drop:
            ranked = [(i, sc) for i, sc in ranked
                      if self.records[i].get("page_type") not in drop]

        results: list[dict] = []
        per_doc: dict[str, int] = {}
        seen_text: set[str] = set()

        for idx, scores in ranked:
            if idx >= len(self.records):
                continue
            rec = self.records[idx]
            if language and rec.get("language") != language:
                continue
            if document_type and rec.get("document_type") != document_type:
                continue
            if page_type and rec.get("page_type") != page_type:
                continue
            url = rec.get("source_url", "")
            if max_per_document and per_doc.get(url, 0) >= max_per_document:
                continue
            # Near-duplicate guard: identical opening text is the same content
            # reached by two chunks.
            fingerprint = rec.get("text", "")[:160]
            if fingerprint in seen_text:
                continue
            seen_text.add(fingerprint)
            per_doc[url] = per_doc.get(url, 0) + 1

            confidence = self._confidence(scores.get("dense_score"),
                                          scores.get("bm25_score"),
                                          scores.get("boosts", {}))
            if confidence < min_confidence:
                continue
            results.append({
                "rank": len(results) + 1,
                "score": round(float(scores["rrf"]), 6),
                "base_score": round(float(scores.get("base_rrf", scores["rrf"])), 6),
                "confidence": confidence,
                "boosts": scores.get("boosts", {}),
                "dense_score": (round(float(scores["dense_score"]), 4)
                                if scores["dense_score"] is not None else None),
                "bm25_score": (round(float(scores["bm25_score"]), 4)
                               if scores["bm25_score"] is not None else None),
                "source_url": url,
                "citation": rec.get("citation") or url,
                "title": rec.get("title", ""),
                "section_heading": rec.get("section_heading", ""),
                "text": rec.get("text", ""),
                "language": rec.get("language", "unknown"),
                "document_type": rec.get("document_type", "html"),
                "chunk_id": rec.get("chunk_id", ""),
                "pdf_page": rec.get("pdf_page"),
                "page_type": rec.get("page_type", "other"),
                "segment": rec.get("segment", "unknown"),
                "has_table": bool(rec.get("has_table")),
                "query_intents": intents,
            })
            if len(results) >= top_k:
                break
        return results


    def retrieve_with_status(self, query: str, min_confidence: float = 0.35,
                             **kw) -> dict:
        """Retrieve, and say plainly whether the results are worth trusting.

        Phase 3 should call this rather than `retrieve`, so "I could not find
        this" is a first-class outcome instead of an agent confidently citing
        whatever happened to rank first.
        """
        hits = self.retrieve(query, **kw)
        if not hits:
            return {"status": "no_results", "results": [],
                    "message": "Nothing matched. The information may not be in "
                               "the corpus, or a filter excluded it."}
        best = max(h["confidence"] for h in hits)
        if best < min_confidence:
            return {"status": "low_confidence", "results": hits,
                    "best_confidence": best, "threshold": min_confidence,
                    "message": f"Best evidence scored {best:.2f}, below "
                               f"{min_confidence:.2f}. Treat as 'not found' "
                               f"rather than answering from these."}
        return {"status": "ok", "results": hits, "best_confidence": best}


_DEFAULT: Retriever | None = None


def retrieve(query: str, top_k: int = 5, language: str | None = None,
             index_dir: Path | str = DEFAULT_INDEX_DIR, **kw) -> list[dict]:
    """Module-level convenience wrapper over a lazily-built shared Retriever."""
    global _DEFAULT
    if _DEFAULT is None or str(_DEFAULT.index.dir) != str(index_dir):
        _DEFAULT = Retriever(index_dir)
    return _DEFAULT.retrieve(query, top_k=top_k, language=language, **kw)
