"""Soft-404 detection: pages that return HTTP 200 but are "not found".

What: learns the site's not-found template by requesting a URL that cannot
      exist, then recognises any page that is really that template.
Inputs: the live site (to learn), or data/raw/soft404.json (to detect).
Outputs: data/raw/soft404.json  {"hash":..., "word_count":..., "sample":...}
Why: many CMSes (Sitecore included) answer unknown paths with 200 OK and a
     generic page instead of a 404. A crawler cannot tell that from a real
     page by status code alone, so guessed URLs quietly enter the corpus as
     dozens of identical documents. They inflate the page count, dominate
     duplicate detection, and waste rendering time - and because they are
     short, they get flagged as client-rendered and never improve.

The giveaway is always the same: many unrelated URLs, byte-identical bodies.
"""
from __future__ import annotations

import hashlib
import json
import re

from bs4 import BeautifulSoup

import config

# A probe path no real site would serve. Kept deterministic so re-runs match.
PROBE_PATHS = [
    "/this-path-should-not-exist-bm-crawler-probe",
    "/en/this-path-should-not-exist-bm-crawler-probe",
]

SOFT404_FILE_NAME = "soft404.json"

# Phrases that identify a not-found page even when its wording varies slightly.
NOT_FOUND_MARKERS = [
    "page not found", "page cannot be found", "page you requested",
    "page doesn't exist", "page does not exist", "404", "not be found",
    "الصفحة غير موجودة", "غير متوفرة",
]

# Signatures of an interstitial standing between the crawler and the content.
CHALLENGE_MARKERS = [
    ("cloudflare", ["cf-browser-verification", "checking your browser",
                    "attention required", "cf_chl", "ray id"]),
    ("bot_protection", ["access denied", "request blocked", "are you a robot",
                        "incapsula", "imperva", "akamai reference"]),
    ("cookie_consent", ["onetrust", "cookie consent", "we use cookies",
                        "accept all cookies", "cookiebot"]),
    ("js_required", ["enable javascript", "javascript is disabled",
                     "requires javascript"]),
]


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def text_hash(html: str) -> str:
    """Hash of visible text only - ignores markup churn between requests."""
    return hashlib.sha256(visible_text(html).lower().encode("utf-8")).hexdigest()[:16]


def classify_interstitial(html: str) -> list[str]:
    """Name any challenge/consent/JS-wall signatures present in the page."""
    low = visible_text(html).lower() + " " + html.lower()[:4000]
    return [name for name, markers in CHALLENGE_MARKERS
            if any(m in low for m in markers)]


def looks_like_not_found(html: str) -> bool:
    low = visible_text(html).lower()
    return any(m in low for m in NOT_FOUND_MARKERS)


def learn(fetcher) -> dict:
    """Request impossible URLs and record what the site answers with."""
    prints = []
    for path in PROBE_PATHS:
        url = config.BASE_URL.rstrip("/") + path
        res = fetcher.fetch(url)
        if not res.raw_path or res.kind != "html":
            continue
        html = open(res.raw_path, encoding="utf-8", errors="replace").read()
        text = visible_text(html)
        prints.append({
            "probe_url": url,
            "status": res.status,
            "hash": text_hash(html),
            "word_count": len(text.split()),
            "sample": text[:300],
            "says_not_found": looks_like_not_found(html),
        })

    soft = [p for p in prints if p["status"] == 200]
    result = {
        "probes": prints,
        # Only a 200 response is a *soft* 404; a real 404 needs no fingerprint.
        "hashes": sorted({p["hash"] for p in soft}),
        "word_counts": sorted({p["word_count"] for p in soft}),
        "detected": bool(soft),
    }
    config.ensure_dirs()
    (config.RAW_DIR / SOFT404_FILE_NAME).write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def load() -> dict:
    path = config.RAW_DIR / SOFT404_FILE_NAME
    if not path.exists():
        return {"hashes": [], "word_counts": [], "detected": False}
    return json.loads(path.read_text(encoding="utf-8"))


def is_soft_404(html: str, fingerprint: dict | None = None) -> bool:
    """True if this page is the site's not-found template rather than content."""
    fp = fingerprint if fingerprint is not None else load()
    if not fp.get("detected"):
        return looks_like_not_found(html)
    return text_hash(html) in set(fp.get("hashes", []))


def main() -> None:
    from fetcher import Fetcher
    result = learn(Fetcher(use_cache=False))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result["detected"]:
        print(f"\n[soft404] This site answers unknown paths with HTTP 200 "
              f"({result['word_counts']} words). Any crawled page matching that "
              f"fingerprint is a not-found page, not content.")
    else:
        print("\n[soft404] Site returns real 404s for unknown paths - good, "
              "no fingerprint needed.")


if __name__ == "__main__":
    main()
