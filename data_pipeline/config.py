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
        # Note: spreadsheet/document extensions are NOT skipped - at banks the
        # fee and rate schedules are published as attachments, so they are
        # routed to the attachment handler instead of being discarded.
        r"\.(jpg|jpeg|png|gif|svg|webp|ico|css|js|woff2?|ttf|eot|mp4)(\?|$)",
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

# Attachments are documents served for download rather than pages to read.
# Extension alone is not enough: Sitecore serves files through a media handler
# (`/-/media/<name>.ashx`, sometimes with no extension at all), so the URL only
# hints at what a thing is - `Fetcher` confirms it from the Content-Type header
# and the file's magic bytes.
ATTACHMENT_URL_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"\.(pdf|xlsx?|docx?|pptx?|csv|zip|rtf)(\?|$)",
        r"\.ashx(\?|$)",     # Sitecore media handler
        r"/-/media/",         # Sitecore media library path
        r"/download",
        r"/attachment",
    )
]

# Content types that are documents, not pages. Mapped to the file extension
# used when storing them in the raw store.
BINARY_CONTENT_TYPES = {
    "application/pdf": ".pdf",
    "application/x-pdf": ".pdf",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/csv": ".csv",
    "application/zip": ".zip",
    "application/octet-stream": ".bin",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tif",
}

# Magic-byte signatures. The Content-Type header lies often enough - especially
# behind media handlers, which love `application/octet-stream` - that the bytes
# are the final authority on what a file actually is.
MAGIC_SIGNATURES = [
    (b"%PDF-", "pdf", ".pdf"),
    (b"PK\x03\x04", "office_or_zip", ".zip"),   # xlsx/docx/pptx are zip containers
    (b"\xd0\xcf\x11\xe0", "office_legacy", ".doc"),  # OLE2: .doc/.xls/.ppt
    (b"\xff\xd8\xff", "image", ".jpg"),
    (b"\x89PNG\r\n", "image", ".png"),
    (b"II*\x00", "image", ".tif"),
    (b"MM\x00*", "image", ".tif"),
    (b"{\\rtf", "rtf", ".rtf"),
]


def is_attachment_url(url: str) -> bool:
    """True if the URL looks like a downloadable document rather than a page."""
    return any(pat.search(url) for pat in ATTACHMENT_URL_PATTERNS)


def sniff_kind(content_type: str, head: bytes) -> tuple[str, str]:
    """Decide what a fetched response really is.

    Returns (kind, file_extension) where kind is one of:
    html | pdf | image | office_or_zip | office_legacy | rtf | other

    Magic bytes win over the header, because a media handler that labels a PDF
    `application/octet-stream` (or worse, `text/html`) is common and would
    otherwise land a binary blob in the HTML corpus as mojibake.
    """
    for sig, kind, ext in MAGIC_SIGNATURES:
        if head.startswith(sig):
            return kind, ext
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in BINARY_CONTENT_TYPES:
        ext = BINARY_CONTENT_TYPES[ct]
        if ct.startswith("image/"):
            return "image", ext
        if ct in ("application/pdf", "application/x-pdf"):
            return "pdf", ext
        return "other", ext
    if ct in ("text/html", "application/xhtml+xml", "text/plain", ""):
        return "html", ".html"
    if ct.startswith("text/"):
        return "html", ".html"
    return "other", ".bin"


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
