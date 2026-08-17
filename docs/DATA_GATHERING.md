# How to gather all the data for the Banque Misr Agentic Research Assistant

This is the strategy behind `data_pipeline/`. Read it before running anything.

## The one decision that matters

**Separate gathering from parsing.** The pipeline runs in two stages that never mix:

```
Stage 1 (network, slow, polite)     Stage 2 (local, fast, repeatable)
discover -> crawl -> raw HTML  ==>  extract -> corpus -> audit
```

Stage 1 touches the bank's servers once and writes raw responses to disk.
Stage 2 reads only from disk. You will rewrite your parser ten times while
tuning retrieval quality; none of those rewrites should cost the site a single
request. If you merge the two stages, every parser bug becomes a re-crawl, and
you will be tempted to crawl fast and aggressively. Don't.

## Stage 0 — Understand before you crawl

Spend 30 minutes with the site open before writing scope rules. Answer:

| Question | Where to look | Why it changes your code |
| --- | --- | --- |
| Is content server-rendered or JS-rendered? | View source (Ctrl+U), not DevTools. If the product table is absent from view-source, it is JS-rendered. | Decides `requests` vs Playwright (`render.py`). |
| Is there a sitemap? | `/robots.txt`, `/sitemap.xml` | A sitemap gives near-complete coverage in one request. |
| What is the URL scheme? | Click through the mega-menu | Sets `ALLOWED_HOSTS`, the `/en/` vs `/ar/` split, and `section_path`. |
| Where do the *numbers* live? | Look for "tariff", "fees", "rates" | At banks these are usually **PDFs**, not HTML. |
| What is behind a login? | Any "Internet Banking" link | Must be excluded from scope — never crawl an auth wall. |

Record the answers. They are literally deliverable §6 of the brief.

## Stage 1 — Discovery: get the URL list before the crawl

Three independent sources, because each one alone has a blind spot:

1. **`robots.txt` + `sitemap.xml`** (`discover.py`) — highest-confidence list of
   real pages. Handles sitemap *index* files recursively.
2. **Navigation links** — the mega-menu on the homepage exposes the product
   hierarchy in one page.
3. **Guessed paths** (`CANDIDATE_PATHS`) — cheap probes for standard bank
   sections. 404s cost nothing; a hit finds an orphan page no link points to.

> Orphan pages are the usual reason an agent answers "not available" about a
> product that plainly exists. Discovery is what prevents that.

## Stage 2 — Crawling: politely, and only once per URL

`crawl.py` does BFS from the seeds. The parts that matter:

- **URL normalisation** (`config.normalise_url`) — strips fragments, tracking
  params and trailing slashes. Without it the same page gets crawled under a
  dozen URLs and your corpus fills with duplicates that wreck retrieval ranking.
- **Scope rules** (`config.in_scope`) — no images, no CSS/JS, no login pages, no
  `/search?` (infinite URL space), no third-party domains.
- **`robots.txt` compliance and a 1.5s delay, single-threaded.** This is a real
  bank's production site. Being slow is the correct engineering choice, and an
  aggressive crawl gets your IP blocked, which ends the project.
- **The link graph is saved** (`outlinks` in the manifest). This answers
  "which pages are related to each other?" from §6 without any extra work.

If the crawler warns that pages `looks_unrendered`, the site is client-rendered:
run `render.py --all-unrendered` to re-fetch those with headless Chromium. The
raw-store layout is identical, so extraction needs no changes.

## Stage 3 — Extraction: build a *citable* corpus, not a text dump

`extract.py` produces one JSON document per page. Three choices carry their weight:

1. **Strip boilerplate.** Nav, header and footer appear on every page. Leave
   them in and every document looks like every other one — retrieval precision
   collapses.
2. **Keep tables as structure**, not flattened prose. Fees, rates and
   eligibility limits live in tables; `"| Gold | EGP 350 |"` survives, whereas
   `"Gold EGP 350"` in a wall of text does not reliably.
3. **Split by heading, and keep the anchor.** The retrieval unit is a section,
   not a page. The agent can then cite
   `.../credit-cards#fees` instead of dumping a whole page — which is exactly
   what the Source Attribution requirement asks for.

Every record carries `url`, `fetched_at` and `content_hash`. Provenance must be
attached at gather time; you cannot reconstruct it later.

## Stage 4 — PDFs (do not skip this)

`pdf_ingest.py` downloads every linked PDF and extracts its text. At Egyptian
banks the tariff schedule, interest rates and T&Cs are PDFs. If you skip them,
the agent will say "fee information not available" on the brief's own headline
example ("compare their fees").

PDFs with almost no extractable text are **scanned images**. They are flagged
`needs_ocr` rather than dropped — so the agent can honestly report "this
document exists but could not be read", which is the Missing Information
Handling requirement, not a bug.

## Stage 5 — Audit: know when you are done

`audit.py` writes `data/reports/site_report.md`: page counts per section, HTTP
status breakdown, most-linked pages, language split, and a **gaps** section
(client-rendered pages, thin pages, missing PDFs). Re-run it after every crawl.

You have gathered enough data when:

- [ ] Every top-level section from the site's own nav appears in the report.
- [ ] The brief's three example tasks can be answered by `grep`ing the corpus.
      If `grep -i "eligibility"` returns nothing, no amount of clever retrieval
      or prompting will save the agent.
- [ ] Table-bearing pages exist (that is where fees and rates are).
- [ ] Zero `looks_unrendered` pages remain.
- [ ] PDF count is non-zero, or you have confirmed the site publishes none.

## Language: pick one, deliberately

The site is bilingual. Every document is tagged `en` / `ar`. Decide early:
English-only is a legitimate, documentable scope decision. Mixing both without
a plan gives you near-duplicate documents that compete in retrieval and an
agent that answers Arabic questions from English pages. Whatever you choose,
write it down as an architectural decision (§9 of the brief).

## Re-crawling and freshness

Rates and offers change. `content_hash` lets you diff crawls and re-extract only
what changed. Keep `fetched_at` visible in the agent's final answers — "as of
2026-08-17" is the honest way to present bank data, and it costs nothing.

## Legal and ethical scope

Public, non-authenticated marketing pages only. Respect `robots.txt`. Never
crawl login/internet-banking areas. Rate-limit. Identify your crawler via
`BM_USER_AGENT` (set it to something with a contact address). This is an
academic project — the burden you place on the site should be invisible.

## Testing without hammering the site

Serve `tests/fixtures/` (or a handful of saved pages) over
`python -m http.server`, point `BM_BASE_URL` at it, and run the full pipeline.
That is how this pipeline was validated end to end — robots blocking, sitemap
discovery, link graph, table extraction and PDF discovery — with zero requests
to banquemisr.com.
