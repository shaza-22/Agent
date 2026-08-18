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

## Ranking (v0.2)

A first run against the real corpus returned news pages for product questions.
Two mechanisms, both fixed:

1. **Plain RRF discards score magnitude.** Every list contributed
   `1/(60+rank)` regardless of match quality, so a useless BM25 top-1 counted
   exactly as much as an excellent dense top-1. Fusion is now weighted
   (dense 1.0, BM25 0.55).
2. **BM25 length normalisation at `b=0.75` favours short documents.** One-line
   news items naming every product outranked the long sections that answer the
   question — and it penalised fee sections precisely because appending a table
   makes them longer. `b` is now 0.4, and terms appearing on more than 35% of
   pages (site-wide branding and nav) are dropped from queries entirely.

On top of that, ranking is metadata-aware:

| Signal | Effect |
| --- | --- |
| Section heading matches query intent (`Eligibility`, `Fees`, `Required Documents`) | strong boost |
| Page is news / about | strong penalty on product questions |
| Corporate page, personal question (or vice-versa) | penalty |
| Section carries a table, query asks about fees/rates | boost |
| PDF attachment, query asks about fees | boost |

Boosts are expressed in units of "ten ranks" (`BOOST_UNIT`) so the constants are
interpretable, and every result reports the `boosts` applied plus its
`base_score`, so a ranking can always be explained.

When a query clearly asks about a product **and** enough product-type results
exist to fill `top_k`, news and about pages are **filtered out**, not merely
penalised — a boost can be outvoted by a strong base score. The guard matters:
if the corpus cannot fill `top_k` with product pages they are kept, because a
weak answer beats an empty one.

Intents are rule-based (no LLM): `eligibility`, `documents`, `fees`, `rates`,
`compare`, `benefits`, with Arabic cues alongside English. A `compare` query
automatically drops to `max_per_document=1` so it spreads across products.

## "No good result"

```python
out = retriever.retrieve_with_status(query, min_confidence=0.35)
out["status"]   # ok | low_confidence | no_results
```

Phase 3 should call this rather than `retrieve`, so "I could not find this" is a
first-class outcome instead of the agent citing whatever ranked first. Each
result also carries `confidence` (0–1). **The threshold is not portable** —
cosine scales differ per model, so calibrate it on your own corpus.

## Diagnostics

```bash
python diagnose_retrieval.py --overview        # what is actually indexed
python diagnose_retrieval.py --content-check   # does the answer EXIST?
python diagnose_retrieval.py --compare         # dense vs bm25 vs hybrid, top 5
```

`--content-check` matters most: reranking cannot surface a section the crawl
never captured. Run it before treating a bad result as a ranking problem.

## Offline fallback

`--model hashing` selects a deterministic hashed bag-of-words embedder that
needs no download. It is **not semantic** — it matches shared tokens and cannot
bridge Arabic to English. It exists so the pipeline and tests run offline and in
CI. Use a real model for actual retrieval quality.

## Tests

```bash
python -m unittest discover -s tests -v     # offline, no model download
```
