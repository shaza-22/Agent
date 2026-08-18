"""Ingest an existing local mirror into the raw store.

What: takes a directory of saved HTML (e.g. from `wget --mirror`) and writes it
      into the pipeline's raw store + manifest, so extract.py and everything
      downstream works unchanged.
Inputs: --dir <mirror root> --base-url https://www.banquemisr.com
Outputs: data/raw/pages/*, data/raw/manifest.jsonl (same format as crawl.py)
Why: two real situations need this. (1) Your network blocks the site but you can
     mirror it from elsewhere. (2) You already mirrored it and re-crawling would
     be a pointless second hit on the bank's servers. The crawl stage is the
     only replaceable part of the pipeline - everything after it is local.

Recommended mirror command (polite, mirrors what crawl.py would fetch):

    wget --mirror --page-requisites=off --convert-links=off \
         --reject 'jpg,jpeg,png,gif,svg,css,js,woff,woff2' \
         --wait=1.5 --random-wait --execute robots=on \
         --user-agent='YourProject/1.0 (you@example.com)' \
         --domains www.banquemisr.com \
         https://www.banquemisr.com/
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import config
from fetcher import url_key, _now

HTML_SUFFIXES = {".html", ".htm", ".xhtml", ""}


def path_to_url(file: Path, root: Path, base_url: str) -> str:
    """Reconstruct the original URL from the mirror's directory layout."""
    rel = file.relative_to(root).as_posix()
    # wget saves example.com/a/b.html for https://example.com/a/b
    for suffix in (".html", ".htm", ".xhtml"):
        if rel.endswith(suffix):
            rel = rel[: -len(suffix)]
            break
    if rel.endswith("/index"):
        rel = rel[: -len("/index")]
    if rel == "index":
        rel = ""
    return config.normalise_url(f"{base_url.rstrip('/')}/{rel}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Import a local HTML mirror")
    ap.add_argument("--dir", required=True, help="mirror root directory")
    ap.add_argument("--base-url", default=config.BASE_URL)
    ap.add_argument("--strip-host-dir", action="store_true",
                    help="mirror root contains a www.banquemisr.com/ folder; use its contents")
    args = ap.parse_args()

    root = Path(args.dir).resolve()
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")
    if args.strip_host_dir:
        candidates = [p for p in root.iterdir() if p.is_dir()]
        if len(candidates) == 1:
            root = candidates[0]
            print(f"[import] using host directory {root}")

    config.ensure_dirs()
    pages_dir = config.RAW_DIR / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    files = [p for p in root.rglob("*")
             if p.is_file() and p.suffix.lower() in HTML_SUFFIXES]
    if not files:
        raise SystemExit(f"no HTML files under {root}")

    imported = skipped = 0
    with open(config.CRAWL_MANIFEST, "w", encoding="utf-8") as manifest:
        for f in sorted(files):
            url = path_to_url(f, root, args.base_url)
            if not config.in_scope(url):
                skipped += 1
                continue
            html = f.read_text(encoding="utf-8", errors="replace")
            key = url_key(url)
            raw_path = pages_dir / f"{key}.html"
            raw_path.write_text(html, encoding="utf-8")
            meta = {"url": url, "final_url": url, "status": 200,
                    "content_type": "text/html", "raw_path": str(raw_path),
                    "fetched_at": _now(), "from_cache": False, "error": None,
                    "imported_from": str(f)}
            (pages_dir / f"{key}.meta.json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8")

            # Recover the link graph so audit.py still works.
            from crawl import extract_links, looks_unrendered
            outlinks = extract_links(html, url)
            manifest.write(json.dumps({
                **meta, "depth": 0, "discovered_from": None,
                "outlinks": outlinks,
                "looks_unrendered": looks_unrendered(html),
            }, ensure_ascii=False) + "\n")
            imported += 1

    # Queue any PDFs the mirrored pages link to, for pdf_ingest.py.
    pdf_urls = set()
    for line in open(config.CRAWL_MANIFEST, encoding="utf-8"):
        for link in json.loads(line).get("outlinks", []):
            if config.PDF_PATTERN.search(link):
                pdf_urls.add(link)
    (config.RAW_DIR / "pdf_urls.json").write_text(
        json.dumps(sorted(pdf_urls), indent=2), encoding="utf-8")

    print(f"[import] {imported} pages imported, {skipped} out of scope, "
          f"{len(pdf_urls)} PDFs queued")
    print(f"[import] now run: python extract.py && python verify_corpus.py")


if __name__ == "__main__":
    main()
