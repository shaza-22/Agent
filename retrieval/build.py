"""Build the retrieval index from the frozen Phase 1 corpus.

Usage:
    python -m retrieval.build
    python -m retrieval.build --model sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
    python -m retrieval.build --model hashing        # offline, for tests only

Reads data/corpus/*.jsonl and writes data/retrieval/. Never writes to
data/corpus/ - the Phase 1 corpus is frozen input.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from .embedders import MODEL_NOTES, build_embedder
from .index import VectorIndex
from .records import load_all

DEFAULT_MODEL = "intfloat/multilingual-e5-small"


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the Phase 2 retrieval index")
    ap.add_argument("--corpus-dir", default="data/corpus")
    ap.add_argument("--index-dir", default="data/retrieval")
    ap.add_argument("--cache-dir", default="data/retrieval/cache")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--pdf-chunk-words", type=int, default=220)
    ap.add_argument("--min-words", type=int, default=8)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--show-model-notes", action="store_true")
    args = ap.parse_args()

    if args.show_model_notes:
        print(MODEL_NOTES)
        return

    corpus_dir = Path(args.corpus_dir)
    if not corpus_dir.exists():
        raise SystemExit(f"corpus not found at {corpus_dir.resolve()} "
                         f"(run from the repo root)")

    print(f"[build] loading corpus from {corpus_dir} (read-only)")
    records = load_all(corpus_dir, min_words=args.min_words,
                       target_words=args.pdf_chunk_words)
    if not records:
        raise SystemExit("no retrieval records produced - is the corpus empty?")

    by_type = Counter(r.document_type for r in records)
    by_lang = Counter(r.language for r in records)
    print(f"[build] {len(records)} retrieval units "
          f"(html={by_type['html']}, pdf={by_type['pdf']})")
    print(f"[build] languages: {dict(by_lang)}")

    print(f"[build] embedding model: {args.model}")
    embedder = build_embedder(args.model)

    idx = VectorIndex(Path(args.index_dir))
    stats = idx.build(records, embedder,
                      cache_dir=None if args.no_cache else Path(args.cache_dir))
    idx.save()

    print(f"[build] embedded now: {stats.embedded_now}, "
          f"reused from cache: {stats.reused_from_cache}")
    print(f"[build] index: {Path(args.index_dir) / VectorIndex.INDEX_FILE} "
          f"({stats.dimension}d, {stats.total_records} vectors)")
    print(f"[build] manifest: {Path(args.index_dir) / VectorIndex.MANIFEST_FILE}")
    print(json.dumps(idx.manifest["build"], indent=2))


if __name__ == "__main__":
    main()
