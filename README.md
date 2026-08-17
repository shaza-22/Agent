# Banque Misr Agentic Research Assistant — data gathering

Stage-one of the project: everything needed to turn <https://www.banquemisr.com>
into a structured, citable corpus the agent can research over.

The strategy and the reasoning behind each choice are in
**[docs/DATA_GATHERING.md](docs/DATA_GATHERING.md)** — read that first.

## Quick start

```bash
pip install -r requirements.txt

cd data_pipeline
python discover.py      # robots.txt + sitemaps + nav  -> data/raw/seeds.json
python crawl.py         # polite BFS crawl             -> data/raw/pages/, manifest.jsonl
python render.py --all-unrendered   # ONLY if crawl.py warns about client-rendered pages
python extract.py       # HTML -> structured documents -> data/corpus/documents.jsonl
python pdf_ingest.py    # tariff/rate PDFs             -> data/corpus/pdf_documents.jsonl
python audit.py         # coverage + site understanding -> data/reports/site_report.md
```

Then read `data/reports/site_report.md`. It tells you what you got, what is
missing, and whether the corpus can actually answer the brief's example tasks.

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
| `audit.py` | Coverage report + site map (deliverable §6) | manifest + corpus | `data/reports/` |

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

## Tests

```bash
python -m unittest discover -s tests -v    # 14 tests, no network, no pytest needed
```

Covers URL normalisation and scope rules, sitemap/sitemap-index parsing, link
extraction, boilerplate stripping, table structure, heading anchors and PDF-link
separation.

## Crawling policy

Public marketing pages only. `robots.txt` is obeyed, requests are serialised
with a delay, login and internet-banking paths are excluded by scope rule, and
the crawler identifies itself via `BM_USER_AGENT`. Set that variable to include
a real contact address before running against the live site.
