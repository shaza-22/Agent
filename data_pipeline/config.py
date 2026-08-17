"""Central configuration for the crawl.

What: single source of truth for host, paths, politeness settings and URL rules.
Inputs: environment variables (all optional, sensible defaults below).
Outputs: module-level constants + helpers imported by every other module.
Why: crawl politeness and scope rules must be defined in exactly one place,
     otherwise different scripts drift and you accidentally hammer the site.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse, urldefrag, urlunparse

# --- Target -----------------------------------------------------------------
BASE_URL = os.environ.get("BM_BASE_URL", "https://www.banquemisr.com")
ALLOWED_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("BM_ALLOWED_HOSTS", "www.banquemisr.com,banquemisr.com").split(",")
    if h.strip()
}

# --- Politeness -------------------------------------------------------------
# One worker, one request at a time, with a delay. This is a public bank site:
# being slow and boring is a feature, not a limitation.
REQUEST_DELAY_SEC = float(os.environ.get("BM_DELAY", "1.5"))
REQUEST_TIMEOUT_SEC = float(os.environ.get("BM_TIMEOUT", "30"))
MAX_RETRIES = int(os.environ.get("BM_MAX_RETRIES", "3"))
MAX_PAGES = int(os.environ.get("BM_MAX_PAGES", "3000"))
MAX_DEPTH = int(os.environ.get("BM_MAX_DEPTH", "6"))
OBEY_ROBOTS = os.environ.get("BM_OBEY_ROBOTS", "1") != "0"

USER_AGENT = os.environ.get(
    "BM_USER_AGENT",
    "BanqueMisrResearchAssistant/1.0 (academic project; contact: set BM_USER_AGENT)",
)

# --- Storage layout ---------------------------------------------------------
DATA_DIR = Path(os.environ.get("BM_DATA_DIR", "data")).resolve()
RAW_DIR = DATA_DIR / "raw"          # immutable HTTP response bodies
PDF_DIR = DATA_DIR / "pdf"          # downloaded PDFs
CORPUS_DIR = DATA_DIR / "corpus"    # extracted structured documents (JSONL)
REPORT_DIR = DATA_DIR / "reports"   # site-understanding / coverage reports

CRAWL_MANIFEST = RAW_DIR / "manifest.jsonl"
CORPUS_FILE = CORPUS_DIR / "documents.jsonl"

# --- URL scope rules --------------------------------------------------------
# Skip anything that is not readable content. Each pattern is a cheap way to
# avoid burning crawl budget on infinite or useless URL space.
SKIP_URL_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"\.(jpg|jpeg|png|gif|svg|webp|ico|css|js|woff2?|ttf|eot|mp4|zip|xlsx?|docx?)(\?|$)",
        r"/(login|signin|logout|register|otp|netbanking|ib/|onlinebanking)",  # auth walls
        r"[?&](utm_|fbclid|gclid)",                                          # tracking noise
        r"#",                                                                # fragments
        r"/search\?",                                                        # infinite search space
    )
]

# Query params that change nothing about the content; dropped during
# normalisation so the same page is not crawled under 10 different URLs.
DROP_QUERY_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                     "utm_content", "fbclid", "gclid", "ref", "_ga"}

PDF_PATTERN = re.compile(r"\.pdf(\?|$)", re.I)


def normalise_url(url: str) -> str:
    """Canonicalise a URL so the same page is only ever crawled once.

    Drops the fragment, lowercases the host, strips tracking params and
    removes a trailing slash (except on the site root).
    """
    url, _ = urldefrag(url.strip())
    p = urlparse(url)
    host = p.netloc.lower()  # keeps any explicit port, which stays part of the identity
    query = "&".join(
        part for part in p.query.split("&")
        if part and part.split("=")[0] not in DROP_QUERY_PARAMS
    )
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((p.scheme.lower(), host, path, p.params, query, ""))


def in_scope(url: str) -> bool:
    """True if the URL belongs to the Banque Misr site and is worth fetching."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    if (p.hostname or "").lower() not in ALLOWED_HOSTS:
        return False
    if PDF_PATTERN.search(url):
        return True  # PDFs are handled by a dedicated downloader, but stay in scope
    return not any(pat.search(url) for pat in SKIP_URL_PATTERNS)


def detect_language(url: str, html_lang: str | None = None) -> str:
    """Banque Misr serves English and Arabic. Tag every document with one."""
    if html_lang:
        low = html_lang.lower()
        if low.startswith("ar"):
            return "ar"
        if low.startswith("en"):
            return "en"
    path = urlparse(url).path.lower()
    if re.search(r"(^|/)(ar|ar-eg)(/|$)", path):
        return "ar"
    if re.search(r"(^|/)(en|en-us|en-eg)(/|$)", path):
        return "en"
    return "unknown"


def ensure_dirs() -> None:
    for d in (RAW_DIR, PDF_DIR, CORPUS_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
