"""Package the corpus into portable, inspectable deliverables.

What: turns the JSONL corpus into CSVs, a SQLite full-text index, and a dataset
      card documenting provenance.
Inputs: data/corpus/*.jsonl, data/raw/manifest.jsonl
Outputs: data/export/{documents,sections,tables,pdfs,links}.csv,
         data/export/corpus.sqlite (FTS5), data/export/CORPUS_CARD.md
Why: JSONL is good for pipelines, bad for humans. The CSVs let a teammate open
     the corpus in Excel and sanity-check the fees by eye; the SQLite FTS index
     gives the agent a working keyword-retrieval tool on day one, with no
     embedding service required; the dataset card records where every row came
     from so results stay auditable.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter

import config


def load_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def write_csv(path, fieldnames, rows) -> int:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        n = 0
        for r in rows:
            w.writerow(r)
            n += 1
    return n


def build_sqlite(path, docs, pdfs) -> None:
    """SQLite + FTS5: a zero-dependency retrieval tool for the agent.

    Sections (not whole pages) are the indexed unit, so a hit returns a precise,
    citable span rather than a page dump.
    """
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE documents(
            url TEXT PRIMARY KEY, title TEXT, language TEXT, doc_type TEXT,
            section_path TEXT, breadcrumbs TEXT, fetched_at TEXT,
            content_hash TEXT, word_count INTEGER, text TEXT);
        CREATE TABLE sections(
            id INTEGER PRIMARY KEY, url TEXT, heading TEXT, level INTEGER,
            anchor TEXT, citation TEXT, text TEXT);
        CREATE TABLE tables_(
            id INTEGER PRIMARY KEY, url TEXT, caption TEXT, headers TEXT,
            n_rows INTEGER, markdown TEXT);
        CREATE TABLE links(source_url TEXT, target_url TEXT, kind TEXT);
        CREATE VIRTUAL TABLE sections_fts USING fts5(
            heading, text, url UNINDEXED, citation UNINDEXED,
            content='sections', content_rowid='id');
    """)

    for d in docs + pdfs:
        db.execute("INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)", (
            d["url"], d.get("title", ""), d.get("language", ""),
            d.get("doc_type", "page"), "/".join(d.get("section_path", [])),
            " > ".join(d.get("breadcrumbs", [])), d.get("fetched_at", ""),
            d.get("content_hash", ""), d.get("word_count", 0), d.get("text", "")))

    sid = 0
    for d in docs:
        for s in d.get("sections", []):
            sid += 1
            citation = d["url"] + (f"#{s['anchor']}" if s.get("anchor") else "")
            db.execute("INSERT INTO sections VALUES (?,?,?,?,?,?,?)", (
                sid, d["url"], s.get("heading", ""), s.get("level", 0),
                s.get("anchor"), citation, s.get("text", "")))
    # PDFs have no heading structure; index each as one section so they are
    # searchable and citable alongside pages.
    for p in pdfs:
        sid += 1
        db.execute("INSERT INTO sections VALUES (?,?,?,?,?,?,?)", (
            sid, p["url"], p.get("title", ""), 0, None, p["url"], p.get("text", "")))

    tid = 0
    for d in docs:
        for t in d.get("tables", []):
            tid += 1
            db.execute("INSERT INTO tables_ VALUES (?,?,?,?,?,?)", (
                tid, d["url"], t.get("caption"), json.dumps(t.get("headers", []),
                ensure_ascii=False), len(t.get("rows", [])), t.get("markdown", "")))

    for d in docs:
        for target in d.get("links", []):
            db.execute("INSERT INTO links VALUES (?,?,?)", (d["url"], target, "page"))
        for target in d.get("pdf_links", []):
            db.execute("INSERT INTO links VALUES (?,?,?)", (d["url"], target, "pdf"))

    db.execute("INSERT INTO sections_fts(sections_fts) VALUES('rebuild')")
    db.commit()
    db.close()


def write_corpus_card(path, docs, pdfs, manifest, counts) -> None:
    langs = Counter(d["language"] for d in docs)
    sections = sum(len(d.get("sections", [])) for d in docs)
    tables = sum(len(d.get("tables", [])) for d in docs)
    dates = sorted(d["fetched_at"] for d in docs if d.get("fetched_at"))
    top = Counter(d["section_path"][0] if d.get("section_path") else "(root)"
                  for d in docs)

    path.write_text(f"""# Banque Misr corpus - dataset card

## Source
- Origin: {config.BASE_URL} (public, non-authenticated pages only)
- Collected by: `data_pipeline/` (discover -> crawl -> extract -> pdf_ingest)
- Crawler identity: `{config.USER_AGENT}`
- robots.txt obeyed: {config.OBEY_ROBOTS} | delay between requests: {config.REQUEST_DELAY_SEC}s
- Collection window: {dates[0] if dates else 'n/a'} .. {dates[-1] if dates else 'n/a'}

## Contents
| Unit | Count |
| --- | --- |
| URLs visited | {len(manifest)} |
| HTML documents | {len(docs)} |
| PDF documents | {len(pdfs)} ({sum(1 for p in pdfs if p.get('needs_ocr'))} unreadable/scanned) |
| Citable sections | {sections} |
| Tables (fees/rates) | {tables} |
| Languages | {', '.join(f'{k}={v}' for k, v in langs.most_common()) or 'n/a'} |

### Coverage by site area
| Area | Documents |
| --- | --- |
{chr(10).join(f'| `{k}` | {v} |' for k, v in top.most_common())}

## Files
| File | Rows | Purpose |
| --- | --- | --- |
| `documents.csv` | {counts['documents']} | one row per page/PDF |
| `sections.csv` | {counts['sections']} | one row per citable section (the retrieval unit) |
| `tables.csv` | {counts['tables']} | one row per table, markdown preserved |
| `pdfs.csv` | {counts['pdfs']} | PDF inventory incl. OCR status |
| `links.csv` | {counts['links']} | the link graph (page relationships) |
| `corpus.sqlite` | - | SQLite + FTS5 keyword index over sections |

## Provenance guarantees
Every row carries `url`, `fetched_at` and `content_hash`. Sections additionally
carry a `citation` (`url#anchor`) that deep-links to the exact heading the text
came from. Nothing in this corpus is generated, inferred or paraphrased - it is
extracted verbatim from the fetched pages.

## Known limitations
- Content behind authentication (internet banking) is out of scope by design.
- Scanned PDFs have no extractable text and are flagged `needs_ocr`; the agent
  must report them as unreadable rather than guessing their contents.
- Rates, fees and offers change. Treat `fetched_at` as the as-of date and
  surface it in agent answers.

## Query example
```sql
SELECT citation, heading, snippet(sections_fts, 1, '[', ']', '...', 12)
FROM sections_fts JOIN sections USING(rowid)
WHERE sections_fts MATCH 'annual fee' LIMIT 5;
```
""", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Export the corpus to CSV + SQLite")
    args = ap.parse_args()

    config.ensure_dirs()
    export_dir = config.DATA_DIR / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    docs = load_jsonl(config.CORPUS_FILE)
    pdfs = load_jsonl(config.CORPUS_DIR / "pdf_documents.jsonl")
    manifest = load_jsonl(config.CRAWL_MANIFEST)
    if not docs and not pdfs:
        raise SystemExit("corpus is empty; run crawl.py and extract.py first")

    counts = {}
    counts["documents"] = write_csv(
        export_dir / "documents.csv",
        ["url", "title", "language", "doc_type", "section_path", "breadcrumbs",
         "fetched_at", "content_hash", "word_count", "n_sections", "n_tables",
         "n_pdf_links"],
        ({**d,
          "section_path": "/".join(d.get("section_path", [])),
          "breadcrumbs": " > ".join(d.get("breadcrumbs", [])),
          "n_sections": len(d.get("sections", [])),
          "n_tables": len(d.get("tables", [])),
          "n_pdf_links": len(d.get("pdf_links", []))}
         for d in docs + pdfs))

    counts["sections"] = write_csv(
        export_dir / "sections.csv",
        ["citation", "url", "heading", "level", "anchor", "language", "text"],
        ({"citation": d["url"] + (f"#{s['anchor']}" if s.get("anchor") else ""),
          "url": d["url"], "heading": s.get("heading", ""), "level": s.get("level", 0),
          "anchor": s.get("anchor") or "", "language": d.get("language", ""),
          "text": s.get("text", "")}
         for d in docs for s in d.get("sections", [])))

    counts["tables"] = write_csv(
        export_dir / "tables.csv",
        ["url", "caption", "headers", "n_rows", "markdown"],
        ({"url": d["url"], "caption": t.get("caption") or "",
          "headers": " | ".join(t.get("headers", [])),
          "n_rows": len(t.get("rows", [])), "markdown": t.get("markdown", "")}
         for d in docs for t in d.get("tables", [])))

    counts["pdfs"] = write_csv(
        export_dir / "pdfs.csv",
        ["url", "title", "language", "pdf_pages", "word_count", "needs_ocr",
         "fetched_at"],
        pdfs)

    counts["links"] = write_csv(
        export_dir / "links.csv", ["source_url", "target_url", "kind"],
        ([{"source_url": d["url"], "target_url": t, "kind": "page"}
          for d in docs for t in d.get("links", [])] +
         [{"source_url": d["url"], "target_url": t, "kind": "pdf"}
          for d in docs for t in d.get("pdf_links", [])]))

    build_sqlite(export_dir / "corpus.sqlite", docs, pdfs)
    write_corpus_card(export_dir / "CORPUS_CARD.md", docs, pdfs, manifest, counts)

    print(f"[export] {export_dir}")
    for k, v in counts.items():
        print(f"[export]   {k}.csv: {v} rows")
    print(f"[export]   corpus.sqlite (FTS5 over {counts['sections']} sections)")
    print(f"[export]   CORPUS_CARD.md")


if __name__ == "__main__":
    main()
