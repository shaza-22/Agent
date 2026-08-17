"""Download and extract text from linked PDFs.

What: fetches every PDF discovered during the crawl and appends one Document
      per PDF (doc_type="pdf") to the corpus.
Inputs: data/raw/pdf_urls.json (from crawl.py) and/or pdf_links in the corpus.
Outputs: PDFs under data/pdf/, plus data/corpus/pdf_documents.jsonl
Why: at banks the numbers the agent needs - tariff schedules, interest rates,
     terms & conditions - live in PDFs, not HTML. Skipping them means the agent
     will answer "fee information not available" on exactly the questions the
     brief uses as its main example.

Requires: pip install pypdf
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re

import config
from fetcher import Fetcher, url_key
from schema import Document


def pdf_to_text(path: str) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("pypdf not installed: pip install pypdf")
    reader = PdfReader(path)
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:                                  # noqa: BLE001
            pages.append("")
    text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(pages)).strip()
    return text, len(reader.pages)


def collect_pdf_urls() -> list[str]:
    urls: set[str] = set()
    listed = config.RAW_DIR / "pdf_urls.json"
    if listed.exists():
        urls.update(json.loads(listed.read_text(encoding="utf-8")))
    if config.CORPUS_FILE.exists():
        for line in open(config.CORPUS_FILE, encoding="utf-8"):
            urls.update(json.loads(line).get("pdf_links", []))
    return sorted(u for u in urls if config.in_scope(u))


def main() -> None:
    ap = argparse.ArgumentParser(description="Download PDFs and extract their text")
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    ap.add_argument("--min-chars", type=int, default=200,
                    help="below this a PDF is probably scanned images -> needs OCR")
    args = ap.parse_args()

    config.ensure_dirs()
    urls = collect_pdf_urls()
    if args.limit:
        urls = urls[: args.limit]
    if not urls:
        print("[pdf] no PDF URLs found; run crawl.py first")
        return

    f = Fetcher()
    out_path = config.CORPUS_DIR / "pdf_documents.jsonl"
    kept = needs_ocr = failed = 0

    with open(out_path, "w", encoding="utf-8") as out:
        for i, url in enumerate(urls, 1):
            res = f.fetch(url, binary=True)
            if not res.ok() or not res.raw_path:
                print(f"[pdf] {i}/{len(urls)} FAILED {url}: {res.error or res.status}")
                failed += 1
                continue
            local = config.PDF_DIR / f"{url_key(url)}.pdf"
            local.write_bytes(open(res.raw_path, "rb").read())
            try:
                text, n_pages = pdf_to_text(str(local))
            except Exception as exc:                        # noqa: BLE001
                print(f"[pdf] {i}/{len(urls)} unreadable {url}: {exc}")
                failed += 1
                continue

            if len(text) < args.min_chars:
                # Scanned PDF. Do NOT drop it silently - record it so the agent
                # can say "this document exists but could not be read".
                needs_ocr += 1

            doc = Document(
                url=url, final_url=res.final_url,
                title=url.rsplit("/", 1)[-1],
                language=config.detect_language(url),
                fetched_at=res.fetched_at,
                content_hash=hashlib.sha256(text.encode()).hexdigest()[:16],
                text=text, doc_type="pdf", word_count=len(text.split()),
            )
            rec = doc.to_record()
            rec["pdf_pages"] = n_pages
            rec["needs_ocr"] = len(text) < args.min_chars
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            kept += 1
            print(f"[pdf] {i}/{len(urls)} {n_pages}p {url}")

    print(f"[pdf] wrote {kept} PDF documents to {out_path} "
          f"({needs_ocr} likely scanned/need OCR, {failed} failed)")


if __name__ == "__main__":
    main()
