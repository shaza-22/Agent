"""HTML -> structured, citable documents.

What: reads the raw store, strips chrome (nav/footer/scripts), and emits one
      JSON document per page with title, breadcrumbs, heading sections, tables
      and outbound PDF links.
Inputs: data/raw/pages/*.html + data/raw/manifest.jsonl
Outputs: data/corpus/documents.jsonl  (one Document per line)
Why: this is the stage that decides what the agent can actually retrieve and
     cite. It is deliberately separate from crawling so you can iterate on
     parsing quality dozens of times without touching the network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString

import config
from schema import Document, Section, Table

# Page furniture that is repeated on every page. Keeping it would make every
# document look similar to every other one and destroy retrieval precision.
BOILERPLATE_SELECTORS = [
    "script", "style", "noscript", "svg", "iframe",
    "nav", "header", "footer", "form",
    "[role=navigation]", "[role=banner]", "[role=contentinfo]",
    ".nav", ".navbar", ".menu", ".mega-menu", ".breadcrumb", ".breadcrumbs",
    ".footer", ".header", ".cookie", ".cookie-banner", ".social",
    ".skip-link", ".search-box", "#onetrust-consent-sdk",
]

MAIN_SELECTORS = ["main", "[role=main]", "article", "#main", "#content",
                  ".main-content", ".content", ".page-content"]


def clean_text(s: str) -> str:
    return re.sub(r"[ \t ]+", " ", s).replace("\r", "").strip()


def collapse_blank_lines(s: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def extract_breadcrumbs(soup: BeautifulSoup) -> list[str]:
    """Read the breadcrumb trail before it gets stripped as boilerplate.

    Breadcrumbs are the site's own statement of how content is organised -
    valuable for the 'how are products organised?' question in the brief.
    """
    for sel in (".breadcrumb", ".breadcrumbs", "[aria-label=breadcrumb]",
                "[itemtype*=BreadcrumbList]", "nav.breadcrumb"):
        node = soup.select_one(sel)
        if node:
            crumbs = [clean_text(a.get_text()) for a in node.find_all(["a", "li", "span"])]
            crumbs = [c for c in crumbs if c and len(c) < 80]
            # de-duplicate while preserving order (li often wraps the a)
            seen, out = set(), []
            for c in crumbs:
                if c.lower() not in seen:
                    seen.add(c.lower())
                    out.append(c)
            if out:
                return out
    return []


def table_to_record(tbl) -> Table:
    rows: list[list[str]] = []
    for tr in tbl.find_all("tr"):
        cells = [clean_text(td.get_text(" ", strip=True))
                 for td in tr.find_all(["th", "td"])]
        if any(cells):
            rows.append(cells)
    if not rows:
        return Table(None, [], [], "")

    header_cells = tbl.find_all("th")
    headers = rows[0] if header_cells else []
    body = rows[1:] if headers else rows

    width = max(len(r) for r in rows)
    def pad(r): return r + [""] * (width - len(r))
    md_lines = []
    if headers:
        md_lines.append("| " + " | ".join(pad(headers)) + " |")
        md_lines.append("| " + " | ".join(["---"] * width) + " |")
    for r in body:
        md_lines.append("| " + " | ".join(pad(r)) + " |")

    cap = tbl.find("caption")
    return Table(
        caption=clean_text(cap.get_text()) if cap else None,
        headers=headers,
        rows=body,
        markdown="\n".join(md_lines),
    )


def split_into_sections(main) -> list[Section]:
    """Walk the main content in document order, splitting on h1..h6.

    Section-level granularity is what lets the agent answer 'what are the fees
    for card X' with a precise citation instead of dumping a whole page.
    """
    sections: list[Section] = []
    current = Section(heading="", level=0, text="", anchor=None)
    buf: list[str] = []

    def flush():
        text = collapse_blank_lines("\n".join(buf))
        if text or current.heading:
            sections.append(Section(current.heading, current.level, text, current.anchor))
        buf.clear()

    for el in main.descendants:
        if isinstance(el, NavigableString):
            continue
        name = getattr(el, "name", None)
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            flush()
            current = Section(
                heading=clean_text(el.get_text(" ", strip=True)),
                level=int(name[1]),
                text="",
                anchor=el.get("id") or (el.find("a").get("name") if el.find("a") else None),
            )
        elif name in ("p", "li", "td", "th", "dd", "dt"):
            t = clean_text(el.get_text(" ", strip=True))
            if t and (not buf or buf[-1] != t):
                buf.append(t)
    flush()
    return [s for s in sections if s.text or s.heading]


def parse_page(html: str, url: str, final_url: str, fetched_at: str) -> Document:
    soup = BeautifulSoup(html, "lxml")

    title = clean_text(soup.title.get_text()) if soup.title else ""
    html_lang = soup.html.get("lang") if soup.html else None
    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_desc = clean_text(meta_desc_tag.get("content", "")) if meta_desc_tag else None
    breadcrumbs = extract_breadcrumbs(soup)

    links, pdf_links = set(), set()
    for a in soup.find_all("a", href=True):
        absolute = config.normalise_url(urljoin(final_url, a["href"]))
        if config.PDF_PATTERN.search(absolute):
            pdf_links.add(absolute)
        elif config.in_scope(absolute):
            links.add(absolute)

    for sel in BOILERPLATE_SELECTORS:
        for node in soup.select(sel):
            node.decompose()

    main = None
    for sel in MAIN_SELECTORS:
        main = soup.select_one(sel)
        if main and len(main.get_text(strip=True)) > 200:
            break
    if main is None or len(main.get_text(strip=True)) < 200:
        main = soup.body or soup

    tables = [t for t in (table_to_record(tbl) for tbl in main.find_all("table")) if t.rows]
    sections = split_into_sections(main)
    text = collapse_blank_lines(main.get_text("\n", strip=True))

    # Section path from the URL: /en/personal/cards/credit-cards
    section_path = [p for p in urlparse(url).path.split("/") if p and p not in ("en", "ar")]

    return Document(
        url=url,
        final_url=final_url,
        title=title,
        language=config.detect_language(url, html_lang),
        fetched_at=fetched_at,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        breadcrumbs=breadcrumbs,
        section_path=section_path,
        meta_description=meta_desc,
        text=text,
        sections=sections,
        tables=tables,
        links=sorted(links),
        pdf_links=sorted(pdf_links),
        doc_type="page",
        word_count=len(text.split()),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract structured documents from raw HTML")
    ap.add_argument("--min-words", type=int, default=20,
                    help="drop near-empty pages (they are usually redirects/shells)")
    args = ap.parse_args()

    config.ensure_dirs()
    if not config.CRAWL_MANIFEST.exists():
        raise SystemExit(f"no manifest at {config.CRAWL_MANIFEST}; run crawl.py first")

    kept = dropped = 0
    with open(config.CORPUS_FILE, "w", encoding="utf-8") as out:
        for line in open(config.CRAWL_MANIFEST, encoding="utf-8"):
            rec = json.loads(line)
            if not rec.get("raw_path") or rec.get("status", 0) >= 400:
                continue
            if "html" not in (rec.get("content_type") or "").lower():
                continue
            try:
                html = open(rec["raw_path"], encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            doc = parse_page(html, rec["url"], rec.get("final_url") or rec["url"],
                             rec.get("fetched_at", ""))
            if doc.word_count < args.min_words:
                dropped += 1
                continue
            out.write(json.dumps(doc.to_record(), ensure_ascii=False) + "\n")
            kept += 1

    print(f"[extract] wrote {kept} documents to {config.CORPUS_FILE} "
          f"({dropped} dropped as too short)")


if __name__ == "__main__":
    main()
