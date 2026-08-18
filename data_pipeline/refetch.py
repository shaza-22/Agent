"""Targeted re-fetch of specific URLs, without re-crawling the site.

What: re-fetches a named set of URLs (explicit list, file, or regex against the
      existing manifest), updating the raw store and the manifest in place, and
      queuing any attachments those pages link to.
Inputs: --url / --url-file / --match <regex> / --discover-links
Outputs: updated data/raw/pages/*, data/raw/manifest.jsonl, pdf_urls.json
Why: when you find a gap (a hub page whose downloads were never captured), you
     need to fix that gap without spending another hour hammering the site for
     pages that are already correct.

Examples:
    python refetch.py --url https://www.banquemisr.com/Home/Pages/Fees --discover-links
    python refetch.py --match '(?i)(fee|tariff|rate|charge)' --discover-links
"""
from __future__ import annotations

import argparse
import json
import re

import config
from crawl import extract_links, looks_unrendered
from fetcher import Fetcher


def load_manifest() -> list[dict]:
    if not config.CRAWL_MANIFEST.exists():
        return []
    return [json.loads(l) for l in
            config.CRAWL_MANIFEST.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-fetch specific URLs only")
    ap.add_argument("--url", nargs="*", default=[])
    ap.add_argument("--url-file", help="file with one URL per line")
    ap.add_argument("--match", help="regex; re-fetch manifest URLs matching it")
    ap.add_argument("--discover-links", action="store_true",
                    help="also queue attachments linked from the re-fetched pages")
    ap.add_argument("--no-cache", action="store_true", default=True,
                    help="ignored: re-fetch always bypasses the cache")
    args = ap.parse_args()

    config.ensure_dirs()
    manifest = load_manifest()
    by_url = {r["url"]: r for r in manifest}

    targets: set[str] = {config.normalise_url(u) for u in args.url}
    if args.url_file:
        targets.update(config.normalise_url(l.strip())
                       for l in open(args.url_file, encoding="utf-8")
                       if l.strip() and not l.startswith("#"))
    if args.match:
        pat = re.compile(args.match)
        targets.update(r["url"] for r in manifest if pat.search(r["url"]))
    if not targets:
        raise SystemExit("no targets (pass --url, --url-file or --match)")

    targets = sorted(t for t in targets if config.in_scope(t))
    print(f"[refetch] {len(targets)} target URLs")

    f = Fetcher(use_cache=False)          # always go to the network
    attachments: set[str] = set()
    kinds: dict[str, int] = {}
    updated = 0

    for i, url in enumerate(targets, 1):
        res = f.fetch(url)
        kinds[res.kind] = kinds.get(res.kind, 0) + 1
        rec = by_url.get(url, {"url": url, "depth": 0, "discovered_from": None,
                               "outlinks": []})
        rec.update({
            "final_url": res.final_url, "status": res.status, "error": res.error,
            "content_type": res.content_type, "kind": res.kind,
            "raw_path": res.raw_path, "fetched_at": res.fetched_at,
            "from_cache": False, "refetched": True,
        })

        if res.ok() and res.kind == "html" and res.raw_path:
            html = open(res.raw_path, encoding="utf-8", errors="replace").read()
            links = extract_links(html, res.final_url)
            rec["outlinks"] = links
            rec["looks_unrendered"] = looks_unrendered(html)
            if args.discover_links:
                attachments.update(l for l in links if config.is_attachment_url(l))
        elif res.kind != "html":
            # A URL that turned out to be a document, not a page. Hand it to the
            # attachment pipeline rather than trying to parse it as HTML.
            rec["looks_unrendered"] = False
            attachments.add(url)

        by_url[url] = rec
        updated += 1
        status = res.error or res.status
        print(f"[refetch] {i}/{len(targets)} [{res.kind}] {status} {url}")

    # Preserve manifest order, appending any URLs that were not in it before.
    order = [r["url"] for r in manifest]
    order += [u for u in by_url if u not in set(order)]
    with open(config.CRAWL_MANIFEST, "w", encoding="utf-8") as out:
        for u in order:
            out.write(json.dumps(by_url[u], ensure_ascii=False) + "\n")

    if attachments:
        existing = set()
        pdf_file = config.RAW_DIR / "pdf_urls.json"
        if pdf_file.exists():
            existing = set(json.loads(pdf_file.read_text(encoding="utf-8")))
        merged = sorted(existing | attachments)
        pdf_file.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        print(f"[refetch] {len(attachments)} attachments queued "
              f"({len(merged)} total in pdf_urls.json)")

    print(f"[refetch] manifest rows updated: {updated}; kinds: {kinds}")
    print("[refetch] now run: python pdf_ingest.py && python extract.py && python audit.py")


if __name__ == "__main__":
    main()
