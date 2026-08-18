"""Breadth-first crawl of the in-scope Banque Misr site.

What: starts from the discovered seeds, fetches each page, extracts its links,
      and keeps going until the frontier is empty or a budget limit is hit.
Inputs: data/raw/seeds.json (from discover.py) or --start URLs.
Outputs: raw HTML under data/raw/pages/, plus data/raw/manifest.jsonl - one
         JSON line per fetched URL (status, depth, discovered-from, timing).
Why: sitemaps are usually incomplete. The crawl fills the gaps and, just as
     importantly, records the *link graph*, which tells you which pages are
     related to each other - a requirement in section 6 of the brief.
"""
from __future__ import annotations

import argparse
import json
from collections import deque
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import config
from fetcher import Fetcher


def extract_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    out: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        url = config.normalise_url(urljoin(base_url, href))
        if config.in_scope(url):
            out.add(url)
    return sorted(out)


def looks_unrendered(html: str) -> bool:
    """Heuristic: an SPA shell has scripts but almost no visible text.

    If this fires a lot, the site is client-rendered and you need render.py
    (Playwright) instead of plain requests.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return len(text) < 400


def load_seeds(seed_file) -> list[str]:
    if not seed_file.exists():
        return [config.BASE_URL]
    data = json.loads(seed_file.read_text(encoding="utf-8"))
    seen, ordered = set(), []
    # sitemap URLs first: they are the highest-confidence list of real pages.
    for bucket in ("sitemap", "nav", "guessed"):
        for u in data.get(bucket, []):
            u = config.normalise_url(u)
            if u not in seen and config.in_scope(u):
                seen.add(u)
                ordered.append(u)
    return ordered


def main() -> None:
    ap = argparse.ArgumentParser(description="Crawl the Banque Misr website")
    ap.add_argument("--start", nargs="*", default=None, help="override seed URLs")
    ap.add_argument("--max-pages", type=int, default=config.MAX_PAGES)
    ap.add_argument("--max-depth", type=int, default=config.MAX_DEPTH)
    ap.add_argument("--no-cache", action="store_true", help="re-fetch even if cached")
    args = ap.parse_args()

    config.ensure_dirs()
    f = Fetcher(use_cache=not args.no_cache)

    seeds = args.start or load_seeds(config.RAW_DIR / "seeds.json")
    frontier: deque[tuple[str, int, str | None]] = deque(
        (config.normalise_url(u), 0, None) for u in seeds
    )
    visited: set[str] = set()
    attachment_urls: set[str] = set()
    unrendered = 0

    with open(config.CRAWL_MANIFEST, "w", encoding="utf-8") as manifest:
        while frontier and len(visited) < args.max_pages:
            url, depth, parent = frontier.popleft()
            if url in visited or depth > args.max_depth:
                continue
            visited.add(url)

            if config.is_attachment_url(url):
                attachment_urls.add(url)   # handled by pdf_ingest.py, not here
                continue

            res = f.fetch(url)
            links: list[str] = []
            flag_unrendered = False
            if res.ok() and res.raw_path and "html" in res.content_type.lower():
                html = open(res.raw_path, encoding="utf-8", errors="replace").read()
                links = extract_links(html, res.final_url)
                flag_unrendered = looks_unrendered(html)
                if flag_unrendered:
                    unrendered += 1
                for link in links:
                    if link not in visited:
                        if config.is_attachment_url(link):
                            attachment_urls.add(link)
                        else:
                            frontier.append((link, depth + 1, url))

            manifest.write(json.dumps({
                "url": url,
                "final_url": res.final_url,
                "status": res.status,
                "error": res.error,
                "depth": depth,
                "discovered_from": parent,
                "content_type": res.content_type,
                "kind": res.kind,
                "raw_path": res.raw_path,
                "fetched_at": res.fetched_at,
                "from_cache": res.from_cache,
                "outlinks": links,
                "looks_unrendered": flag_unrendered,
            }, ensure_ascii=False) + "\n")
            manifest.flush()

            if len(visited) % 25 == 0:
                print(f"[crawl] {len(visited)} pages, frontier={len(frontier)}")

    (config.RAW_DIR / "pdf_urls.json").write_text(
        json.dumps(sorted(attachment_urls), indent=2), encoding="utf-8"
    )
    print(f"[crawl] done: {len(visited)} URLs, "
          f"{len(attachment_urls)} attachments queued")
    if unrendered:
        print(f"[crawl] WARNING: {unrendered} pages looked client-rendered. "
              f"Re-fetch those with render.py before extracting.")


if __name__ == "__main__":
    main()
