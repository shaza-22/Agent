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

DEFAULT_INDEX_DIR = Path("data/retrieval")
RRF_K = 60          # standard RRF damping constant


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
              lexical: list[tuple[int, float]]) -> dict[int, dict]:
        """Reciprocal Rank Fusion over two rankings on incomparable scales."""
        fused: dict[int, dict] = {}
        for rank, (idx, score) in enumerate(dense):
            fused.setdefault(idx, {"dense_score": None, "bm25_score": None,
                                   "rrf": 0.0})
            fused[idx]["dense_score"] = score
            fused[idx]["rrf"] += 1.0 / (RRF_K + rank + 1)
        for rank, (idx, score) in enumerate(lexical):
            fused.setdefault(idx, {"dense_score": None, "bm25_score": None,
                                   "rrf": 0.0})
            fused[idx]["bm25_score"] = score
            fused[idx]["rrf"] += 1.0 / (RRF_K + rank + 1)
        return fused

    def retrieve(self, query: str, top_k: int = 5, language: str | None = None,
                 document_type: str | None = None, max_per_document: int = 2,
                 mode: str = "hybrid", depth: int | None = None) -> list[dict]:
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
            ranked = sorted(self._fuse(dense, lexical).items(),
                            key=lambda kv: -kv[1]["rrf"])

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

            results.append({
                "rank": len(results) + 1,
                "score": round(float(scores["rrf"]), 6),
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
            })
            if len(results) >= top_k:
                break
        return results


_DEFAULT: Retriever | None = None


def retrieve(query: str, top_k: int = 5, language: str | None = None,
             index_dir: Path | str = DEFAULT_INDEX_DIR, **kw) -> list[dict]:
    """Module-level convenience wrapper over a lazily-built shared Retriever."""
    global _DEFAULT
    if _DEFAULT is None or str(_DEFAULT.index.dir) != str(index_dir):
        _DEFAULT = Retriever(index_dir)
    return _DEFAULT.retrieve(query, top_k=top_k, language=language, **kw)
