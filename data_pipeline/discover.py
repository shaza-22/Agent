"""URL discovery: find out what pages exist before crawling them.

What: collects candidate URLs from robots.txt, sitemap.xml (recursively, incl.
      sitemap index files), a set of well-known section paths, and the site's
      own navigation menu.
Inputs: the live site (via `Fetcher`).
Outputs: data/raw/seeds.json  -> {"sitemap": [...], "nav": [...], "guessed": [...]}
Why: a blind link-following crawl finds pages slowly and misses orphan pages
     (many product/tariff pages are only linked from mega-menus or PDFs).
     Sitemaps give you near-complete coverage in one cheap request.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

import config
import soft404
from fetcher import Fetcher

# Paths worth probing even if no sitemap exists. Adjust after you see the
# real site structure - this is a starting guess, not an assumption.
CANDIDATE_PATHS = [
    "/", "/en", "/ar",
    "/en/personal", "/en/personal/accounts", "/en/personal/cards",
    "/en/personal/cards/credit-cards", "/en/personal/cards/debit-cards",
    "/en/personal/cards/prepaid-cards", "/en/personal/loans",
    "/en/personal/deposits", "/en/personal/certificates",
    "/en/personal/digital-banking", "/en/personal/offers",
    "/en/corporate", "/en/sme", "/en/islamic", "/en/wealth",
    "/en/about-us", "/en/contact-us", "/en/branches", "/en/atm",
    "/en/rates", "/en/exchange-rates", "/en/interest-rates", "/en/tariff",
    "/en/faq", "/en/terms-and-conditions", "/en/news", "/en/careers",
    "/sitemap", "/en/sitemap",
]

SITEMAP_CANDIDATES = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
                      "/en/sitemap.xml", "/ar/sitemap.xml"]

_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """Return (page_urls, nested_sitemap_urls) from a sitemap or sitemap index."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError:
        return [], []
    tag = root.tag.split("}")[-1]
    locs = [e.text.strip() for e in root.iter() if e.tag.split("}")[-1] == "loc" and e.text]
    if tag == "sitemapindex":
        return [], locs
    return locs, []


def collect_sitemap_urls(f: Fetcher, max_sitemaps: int = 50) -> list[str]:
    """Walk robots.txt sitemaps + common sitemap paths, following index files."""
    queue = list(f.sitemaps_from_robots())
    queue += [config.BASE_URL.rstrip("/") + p for p in SITEMAP_CANDIDATES]
    seen_sitemaps: set[str] = set()
    pages: set[str] = set()

    while queue and len(seen_sitemaps) < max_sitemaps:
        sm = config.normalise_url(queue.pop(0))
        if sm in seen_sitemaps:
            continue
        seen_sitemaps.add(sm)
        res = f.fetch(sm)
        if not res.ok() or not res.raw_path:
            continue
        text = open(res.raw_path, encoding="utf-8", errors="replace").read()
        if "<" not in text:
            continue
        page_urls, nested = parse_sitemap(text)
        pages.update(u for u in page_urls if config.in_scope(u))
        queue.extend(nested)

    print(f"[discover] {len(seen_sitemaps)} sitemap(s) checked -> {len(pages)} URLs")
    return sorted(pages)


def collect_nav_urls(f: Fetcher) -> list[str]:
    """Pull every link out of the homepage(s) - this captures the mega-menu."""
    found: set[str] = set()
    for path in ("/", "/en", "/ar"):
        res = f.fetch(config.BASE_URL.rstrip("/") + path)
        if not res.ok() or not res.raw_path:
            continue
        html = open(res.raw_path, encoding="utf-8", errors="replace").read()
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            url = config.normalise_url(_absolutise(res.final_url, a["href"]))
            if config.in_scope(url):
                found.add(url)
    print(f"[discover] navigation links -> {len(found)} URLs")
    return sorted(found)


def _absolutise(base: str, href: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, href)


def main() -> None:
    config.ensure_dirs()
    f = Fetcher()
    # Learn how the site answers a URL that cannot exist. Without this, guessed
    # paths that return "200 + page not found" enter the corpus as real pages.
    fp = soft404.learn(f)
    if fp["detected"]:
        print(f"[discover] site serves SOFT 404s (HTTP 200, {fp['word_counts']} "
              f"words). Pages matching that fingerprint will be excluded.")
    else:
        print("[discover] site returns real 404s for unknown paths")

    seeds = {
        "sitemap": collect_sitemap_urls(f),
        "nav": collect_nav_urls(f),
        "guessed": [config.BASE_URL.rstrip("/") + p for p in CANDIDATE_PATHS],
    }
    out = config.RAW_DIR / "seeds.json"
    out.write_text(json.dumps(seeds, indent=2, ensure_ascii=False), encoding="utf-8")
    total = len({u for v in seeds.values() for u in v})
    print(f"[discover] wrote {out} ({total} unique candidate URLs)")


if __name__ == "__main__":
    main()
