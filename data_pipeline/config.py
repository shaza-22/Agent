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
from urllib.parse import urlparse, urldefrag, urlunparse, unquote_plus, quote

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
# Random extra wait added to every request. A perfectly regular 1.5s cadence is
# itself a bot signature; jitter makes the traffic look less machine-timed and
# spreads load. Cost is negligible, so it is on by default.
REQUEST_JITTER_SEC = float(os.environ.get("BM_JITTER", "1.0"))
# Headless-browser renders are far heavier than a plain GET (each pulls every
# subresource), so they get their own, slower pacing.
RENDER_DELAY_SEC = float(os.environ.get("BM_RENDER_DELAY", "5.0"))
# Consecutive block pages before the run stops. Continuing past a block just
# fills the corpus with block pages and deepens whatever triggered it.
MAX_CONSECUTIVE_BLOCKS = int(os.environ.get("BM_MAX_BLOCKS", "3"))
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
DROP_QUERY_PARAMS = {
    # Marketing / analytics
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "_ga", "_gl", "mc_cid", "mc_eid",
    # Sitecore: csrt is a per-session anti-CSRF token regenerated on every
    # visit, so the same page appears under unlimited distinct URLs. Left in,
    # it multiplies the corpus with byte-identical duplicates.
    "csrt", "sc_camp", "sc_trk", "sc_device", "sc_debug", "sc_prof",
    "sc_ritm", "sc_rb", "cshid", "timestamp", "_t",
}

# IIS/Sitecore treat paths case-insensitively, so /EN/Personal and /en/personal
# are one page. Set BM_CASE_SENSITIVE_PATHS=1 for a case-sensitive origin
# (nginx/apache on Linux), where lowercasing would merge genuinely distinct URLs.
CASE_SENSITIVE_PATHS = os.environ.get("BM_CASE_SENSITIVE_PATHS", "0") == "1"

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

    Handles every way one page can wear many URLs:
      - fragment dropped, host lowercased
      - session/tracking params stripped (Sitecore `csrt` above all)
      - remaining params sorted, so ?a=1&b=2 and ?b=2&a=1 are one URL
      - duplicate slashes collapsed, trailing slash removed
      - percent-encoding normalised (%20 and + both become a literal space
        before re-encoding, so the three spellings of a path converge)
      - path lowercased unless CASE_SENSITIVE_PATHS is set

    Without this, one page reachable under dozens of variants becomes dozens of
    byte-identical documents that compete with each other in retrieval.
    """
    url, _ = urldefrag(url.strip())
    p = urlparse(url)
    host = p.netloc.lower()  # keeps any explicit port, which stays part of the identity

    # Query: drop noise, keep order stable.
    kept = []
    for part in p.query.split("&"):
        if not part:
            continue
        name = part.split("=")[0]
        if name.lower() in DROP_QUERY_PARAMS:
            continue
        kept.append(part)
    query = "&".join(sorted(kept))

    # Path: decode -> collapse -> re-encode, so spelling variants converge.
    path = unquote_plus(p.path or "/")
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if not CASE_SENSITIVE_PATHS:
        path = path.lower()
    path = quote(path, safe="/:@!$&'()*+,;=~-._")

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


def detect_language(url: str, html_lang: str | None = None,
                    text: str | None = None, meta_lang: str | None = None) -> str:
    """Tag a document as `en`, `ar`, or `unknown`.

    Tried in order of reliability:
      1. an explicit declaration (`<html lang>`, `xml:lang`, content-language,
         `og:locale`)
      2. the URL, covering the several ways a site marks language
      3. **the script the text is actually written in**

    Step 3 is what makes this work. Earlier versions relied on step 2 alone and
    tagged almost everything `unknown`, because this site's real URLs look like
    `/home/pages/fees` - no language segment anywhere. Arabic and English are
    written in different scripts, so counting characters answers the question
    directly instead of inferring it from a naming convention that may not exist.
    """
    for declared in (html_lang, meta_lang):
        if declared:
            low = declared.strip().lower()
            if low.startswith("ar"):
                return "ar"
            if low.startswith("en"):
                return "en"

    parsed = urlparse(url)
    path = parsed.path.lower()
    query = parsed.query.lower()
    if re.search(r"(^|/)(ar|ar-eg|ar-sa|arabic)(/|-|_|$)", path) or \
            re.search(r"(^|[?&])(sc_)?lang(uage)?=ar", query):
        return "ar"
    if re.search(r"(^|/)(en|en-us|en-eg|en-gb|english)(/|-|_|$)", path) or \
            re.search(r"(^|[?&])(sc_)?lang(uage)?=en", query):
        return "en"

    if text:
        return detect_script_language(text)
    return "unknown"


# Arabic block, plus the supplement and presentation forms used for ligatures.
_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def detect_script_language(text: str, threshold: float = 0.15) -> str:
    """Decide language from the script the text is written in.

    Bilingual pages carry some of both (an English nav on an Arabic page, a
    bank name in Latin script on an Arabic one), so this is a ratio rather than
    a presence test. Arabic passes a low bar because a page with a meaningful
    amount of Arabic prose is an Arabic page, even with Latin branding on it.
    """
    sample = text[:20000]
    arabic = len(_ARABIC_RE.findall(sample))
    latin = len(_LATIN_RE.findall(sample))
    total = arabic + latin
    if total < 20:
        return "unknown"
    if arabic / total >= threshold:
        return "ar"
    if latin / total >= 0.5:
        return "en"
    return "unknown"


def ensure_dirs() -> None:
    for d in (RAW_DIR, PDF_DIR, CORPUS_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
