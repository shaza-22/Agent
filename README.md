# Banque Misr Agentic Research Assistant — data gathering

Stage-one of the project: everything needed to turn <https://www.banquemisr.com>
into a structured, citable corpus the agent can research over.

The strategy and the reasoning behind each choice are in
**[docs/DATA_GATHERING.md](docs/DATA_GATHERING.md)** — read that first.

## Quick start

```bash
pip install -r requirements.txt
export BM_USER_AGENT="YourProject/1.0 (you@example.com)"   # set a real contact
./run_pipeline.sh
```

That runs discover -> crawl -> extract -> pdf_ingest -> audit -> export ->
verify, and prints where to look. To run the stages individually:

```bash
cd data_pipeline
python discover.py       # robots.txt + sitemaps + nav   -> data/raw/seeds.json
python crawl.py          # polite BFS crawl              -> data/raw/pages/, manifest.jsonl
python render.py --all-unrendered   # ONLY if crawl.py warns about client-rendered pages
python extract.py        # HTML -> structured documents  -> data/corpus/documents.jsonl
python pdf_ingest.py     # tariff/rate PDFs              -> data/corpus/pdf_documents.jsonl
python audit.py          # site understanding + coverage -> data/reports/site_report.md
python export.py         # CSVs + SQLite FTS + card      -> data/export/
python verify_corpus.py  # the trust gate                -> data/reports/verification.md
```

### Already have a mirror, or the site is unreachable from your machine?

Mirror it once with `wget` (the exact polite command is in `import_local.py`'s
docstring), then feed the mirror to the same pipeline:

```bash
./run_pipeline.sh --from-mirror ./mirror
```

Only the crawl stage is replaced; extraction, verification and export are
identical.

## Is the corpus trustworthy?

`verify_corpus.py` is the gate, and it exits non-zero on failure so you can run
it in CI. It checks that:

- every record carries `url`, `fetched_at`, `content_hash` and `language`
- **every citation URL actually returned 2xx during the crawl** — no citing a page that 404s
- no empty/shell documents (the signature of an unrendered JS page)
- no duplicate content competing in retrieval
- tables are well formed (they carry the fees and rates)
- every linked PDF was fetched; scanned ones are flagged `needs_ocr`, never dropped silently
- the corpus is fresh, and the crawl finished without errors
- **the brief's example tasks are answerable** — no retrieval strategy recovers a term
  the corpus never gathered

Nothing in the corpus is generated, inferred or paraphrased. Every field is
extracted verbatim from a fetched page, and `data/export/CORPUS_CARD.md`
records where it all came from.

## Modules

| File | What it does | Inputs | Outputs |
| --- | --- | --- | --- |
| `config.py` | Scope rules, politeness settings, URL normalisation | env vars | constants + helpers |
| `fetcher.py` | The only network path: robots.txt, rate limit, retries, on-disk cache | URL | `data/raw/pages/<sha1>.html` |
| `discover.py` | Finds URLs before crawling (sitemaps, nav, probes) | live site | `data/raw/seeds.json` |
| `crawl.py` | BFS crawl, records the link graph | seeds | `data/raw/manifest.jsonl` |
| `render.py` | Headless-browser re-fetch for JS-rendered pages | flagged URLs | same raw store |
| `schema.py` | The `Document` record shape (url, sections, tables, provenance) | — | dataclasses |
| `extract.py` | HTML → titles, breadcrumbs, heading sections, tables, PDF links | raw store | `data/corpus/documents.jsonl` |
| `pdf_ingest.py` | Downloads + reads PDFs, flags scanned ones as `needs_ocr` | PDF URLs | `data/corpus/pdf_documents.jsonl` |
| `audit.py` | Coverage report + site map (deliverable §6) | manifest + corpus | `data/reports/site_report.md` |
| `verify_corpus.py` | **Trust gate** — 13 checks, exits non-zero on failure | corpus | `data/reports/verification.md` |
| `export.py` | CSVs, SQLite FTS5 index, dataset card | corpus | `data/export/` |
| `import_local.py` | Ingests an existing `wget` mirror instead of crawling | mirror dir | `data/raw/` |
| `refetch.py` | Re-fetches specific URLs only, no full re-crawl | URL list/regex | updated `data/raw/` |
| `diagnose.py` | Compares manifest against bytes on disk; finds stale state | manifest | stdout table |

## Configuration

All settings are environment variables with working defaults (see `config.py`):

| Variable | Default | Notes |
| --- | --- | --- |
| `BM_BASE_URL` | `https://www.banquemisr.com` | point at a local fixture server to test |
| `BM_ALLOWED_HOSTS` | `www.banquemisr.com,banquemisr.com` | comma-separated hostnames |
| `BM_DELAY` | `1.5` | seconds between requests — do not lower for the real site |
| `BM_MAX_PAGES` / `BM_MAX_DEPTH` | `3000` / `6` | crawl budget |
| `BM_OBEY_ROBOTS` | `1` | leave it on |
| `BM_USER_AGENT` | project string | **set this to include a contact address** |
| `BM_DATA_DIR` | `data` | where everything is written |

## Corpus record

One JSON object per line in `data/corpus/documents.jsonl`:

```jsonc
{
  "url": "https://www.banquemisr.com/en/personal/cards/credit-cards",  // the citation
  "title": "Credit Cards | Banque Misr",
  "language": "en",
  "fetched_at": "2026-08-17T00:00:00Z",     // freshness, for staleness checks
  "content_hash": "a1b2c3d4e5f6a7b8",       // change detection between crawls
  "breadcrumbs": ["Home", "Personal", "Credit Cards"],
  "section_path": ["personal", "cards", "credit-cards"],
  "sections": [{"heading": "Fees and Charges", "level": 2,
                "anchor": "fees", "text": "..."}],   // anchor -> deep-linkable citation
  "tables": [{"headers": ["Card", "Annual Fee"], "rows": [["Gold", "EGP 350"]],
              "markdown": "| Card | Annual Fee |\n..."}],
  "pdf_links": ["https://www.banquemisr.com/docs/tariff-2026.pdf"],
  "links": ["..."],                          // the link graph
  "doc_type": "page",
  "word_count": 70
}
```

`sections[].anchor` is what lets the agent cite `…/credit-cards#fees` rather
than a whole page — the Source Attribution requirement in the brief.

## Retrieval, day one

`data/export/corpus.sqlite` ships an FTS5 index over **sections**, so a hit
returns a precise citation rather than a page dump — no embedding service
needed to get started:

```sql
SELECT s.citation, s.heading, snippet(sections_fts, 1, '[', ']', '...', 12)
FROM sections_fts JOIN sections s ON s.id = sections_fts.rowid
WHERE sections_fts MATCH 'annual fee' LIMIT 5;
```

```
…/personal/cards/credit-cards#fees | Fees and Charges | …[Annual] [Fee] …
```

## Tests

```bash
python -m unittest discover -s tests -v    # 22 tests, no network, no pytest needed
```

Covers URL normalisation and scope rules, sitemap/sitemap-index parsing, link
extraction, boilerplate stripping, table structure, heading anchors, PDF-link
separation, mirror-path→URL reconstruction, and that the verification gate
genuinely fails on untrustworthy corpora (missing provenance, unresolvable
citations, shell pages, unanswerable corpora).

The full pipeline is validated end to end by serving a synthetic fixture site
over `python -m http.server` and running every stage against it — zero requests
to banquemisr.com.

## Attachments (where the fee and rate numbers actually live)

Banque Misr runs Sitecore, which serves downloads through a media handler:
`/-/media/<name>.ashx`, sometimes with no extension at all, and frequently
under a misleading `Content-Type` (`application/octet-stream`, or even
`text/html`, for a file that is really a PDF).

So file type is decided from the **response**, not the URL: `config.sniff_kind`
checks magic bytes first and falls back to the header. `.ashx`, `/-/media/`,
spreadsheets and Word documents are all routed to `pdf_ingest.py`, which
classifies each file and extracts what it can. Attachments that turn out to be
**scanned images have no extractable text and are flagged `needs_ocr`** — they
are recorded, never silently dropped.

Note that attachments land in `data/corpus/pdf_documents.jsonl`, *not*
`documents.jsonl`. Grep both:

```bash
cat data/corpus/*.jsonl | grep -ic "annual fee"
```

## Debugging a stage that appears to have done nothing

```bash
python diagnose.py --all-unrendered     # manifest vs bytes on disk
python refetch.py --match '(?i)(fee|tariff|rate|charge)' --discover-links
```

`render.py` writes its results back into `manifest.jsonl` and recomputes
`looks_unrendered` against the rendered DOM. Without that write-back the flag
stays frozen at its crawl-time value and `audit.py` reports the same count
forever, however many times you render.

## Crawling policy

Public marketing pages only. `robots.txt` is obeyed, requests are serialised
with a delay, login and internet-banking paths are excluded by scope rule, and
the crawler identifies itself via `BM_USER_AGENT`. Set that variable to include
a real contact address before running against the live site.
