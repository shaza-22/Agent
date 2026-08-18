"""Download and extract text from linked attachments (PDF, Office, images).

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
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # ImportError, or a native dep that panics
        raise SystemExit(
            f"cannot load pypdf ({exc}).\n"
            "  Install it with:  pip install pypdf\n"
            "  If it is installed and still fails, its 'cryptography' native\n"
            "  dependency is broken in this environment; try:\n"
            "    pip install --force-reinstall cryptography pypdf"
        ) from exc
    reader = PdfReader(path)
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:                                  # noqa: BLE001
            pages.append("")
    text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(pages)).strip()
    return text, len(reader.pages)


def classify_local(path: str) -> tuple[str, str]:
    """Identify a downloaded file from its magic bytes. Returns (kind, ext)."""
    with open(path, "rb") as fh:
        head = fh.read(16)
    return config.sniff_kind("", head)


def office_to_text(path: str, kind: str) -> str:
    """Best-effort text from spreadsheet/doc attachments.

    Fee schedules are frequently published as .xlsx. Extraction is attempted
    with optional libraries; if they are absent the file is still recorded, so
    the agent knows the document exists rather than silently missing it.
    """
    try:
        if kind == "office_or_zip":
            import zipfile
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                if any(n.startswith("xl/") for n in names):
                    from openpyxl import load_workbook
                    wb = load_workbook(path, read_only=True, data_only=True)
                    rows = []
                    for ws in wb.worksheets:
                        rows.append(f"# sheet: {ws.title}")
                        for row in ws.iter_rows(values_only=True):
                            cells = [str(c) for c in row if c is not None]
                            if cells:
                                rows.append(" | ".join(cells))
                    return "\n".join(rows)
                if any(n.startswith("word/") for n in names):
                    import docx
                    return "\n".join(p.text for p in docx.Document(path).paragraphs)
    except Exception as exc:                                    # noqa: BLE001
        print(f"[pdf]   could not read {path}: {exc}")
        return ""
    return ""


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
                    help="below this an attachment is scanned/unparsable -> needs OCR")
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
    kept = needs_ocr = failed = skipped_html = 0
    by_kind: dict[str, int] = {}

    with open(out_path, "w", encoding="utf-8") as out:
        for i, url in enumerate(urls, 1):
            res = f.fetch(url)   # auto-detect: header + magic bytes decide the type
            if not res.ok() or not res.raw_path:
                print(f"[pdf] {i}/{len(urls)} FAILED {url}: {res.error or res.status}")
                failed += 1
                continue
            kind, ext = classify_local(res.raw_path)
            local = config.PDF_DIR / f"{url_key(url)}{ext}"
            local.write_bytes(open(res.raw_path, "rb").read())

            n_pages = 0
            text = ""
            if kind == "pdf":
                try:
                    text, n_pages = pdf_to_text(str(local))
                except Exception as exc:                    # noqa: BLE001
                    print(f"[pdf] {i}/{len(urls)} unreadable PDF {url}: {exc}")
                    failed += 1
                    continue
            elif kind == "image":
                # A scanned fee schedule published as an image. There is no text
                # to extract; flag it loudly so OCR can be decided deliberately.
                text = ""
            elif kind in ("office_or_zip", "office_legacy"):
                text = office_to_text(str(local), kind)
            elif kind == "html":
                # Attachment URL that actually served a page - let extract.py
                # handle it instead of storing it here as a broken document.
                print(f"[pdf] {i}/{len(urls)} SKIP (served HTML, not a file) {url}")
                skipped_html += 1
                continue

            if len(text) < args.min_chars:
                # Scanned or unparsable. Do NOT drop it silently - record it so
                # the agent can say "this document exists but could not be read".
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
            rec["attachment_kind"] = kind
            rec["needs_ocr"] = len(text) < args.min_chars
            rec["local_path"] = str(local)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            kept += 1
            by_kind[kind] = by_kind.get(kind, 0) + 1
            flag = " NEEDS-OCR" if rec["needs_ocr"] else ""
            print(f"[pdf] {i}/{len(urls)} [{kind}] {n_pages}p "
                  f"{len(text.split())}w{flag} {url}")

    print(f"\n[pdf] wrote {kept} attachment documents to {out_path}")
    print(f"[pdf] by type: {by_kind}")
    print(f"[pdf] {needs_ocr} have no extractable text (scanned/unparsable -> OCR), "
          f"{failed} failed, {skipped_html} served HTML instead of a file")
    if needs_ocr:
        print("[pdf] OCR is needed for those; until then the agent must report "
              "them as existing-but-unreadable, never guess their contents.")


if __name__ == "__main__":
    main()
