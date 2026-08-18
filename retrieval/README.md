# Phase 2 — retrieval layer

Read-only over the frozen Phase 1 corpus (`data/corpus/`). Nothing here writes
to it.

## Build and query

```bash
pip install -r requirements.txt
python -m retrieval.build                  # -> data/retrieval/
python retrieve.py "What documents do I need to open an account?"
python retrieve.py "ما هي رسوم بطاقات بنك مصر؟" --language ar
python -m retrieval.build --show-model-notes   # why this model
```

## API (this is what Phase 3 imports)

```python
from retrieval import retrieve

hits = retrieve("annual fee credit card", top_k=5, language="en")
for h in hits:
    print(h["score"], h["citation"], h["section_heading"])
```

`Retriever(index_dir)` gives the same thing with an explicit index and lets you
keep one instance alive. Parameters: `top_k`, `language`, `document_type`,
`max_per_document`, `mode` (`hybrid` | `dense` | `bm25`).

Every result carries `source_url`, `citation` (url + `#anchor`), `title`,
`section_heading`, `text`, `language`, `document_type`, `chunk_id`, `score`,
`dense_score`, `bm25_score`, and `pdf_page` where known.

## Design decisions

**Retrieval is hybrid.** Dense vectors for meaning, BM25 for exact terms, fused
with Reciprocal Rank Fusion. Neither alone is adequate: dense search misses
`EGP 350` because numbers carry little semantic signal, while BM25 misses
*"what do I need to open an account"* → *"Required Documents"* because they
share no words. RRF rather than weighted scores, because cosine and BM25 are
not on comparable scales.

**Sections are the retrieval unit** for HTML — Phase 1 already split by heading
and kept the anchors, so a hit cites `…/credit-cards#fees` rather than a page.
**Table markdown is appended to its section**, otherwise a query for "annual
fee" can never match a page whose fees live in a table.

**PDFs are chunked** into ~220-word paragraph-aligned windows with 40-word
overlap, so a fact on a boundary is not lost to both neighbours. Page numbers
are *estimated* from position and labelled as such. Scanned attachments with no
extractable text are **not indexed** — they are recorded upstream as
`needs_ocr`, so the agent can say the document exists and could not be read.

**`max_per_document`** (default 2) caps how many chunks one page contributes.
Broad queries like *"compare the credit cards"* otherwise return five sections
of one page instead of five products.

**FAISS over Chroma**: a flat cosine index over a few thousand vectors is one
FAISS call plus a JSONL sidecar. Chroma would add a database and a client
lifecycle for no gain at this size.

**Embeddings are cached** in `data/retrieval/cache/<fingerprint>.npz`, keyed by
content hash and embedding-space fingerprint, so a rebuild re-embeds only what
changed and switching models does not corrupt the cache.

## Offline fallback

`--model hashing` selects a deterministic hashed bag-of-words embedder that
needs no download. It is **not semantic** — it matches shared tokens and cannot
bridge Arabic to English. It exists so the pipeline and tests run offline and in
CI. Use a real model for actual retrieval quality.

## Tests

```bash
python -m unittest discover -s tests -v     # offline, no model download
```
