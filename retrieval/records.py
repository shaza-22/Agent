"""Turn the frozen Phase 1 corpus into retrieval records.

What: loads documents.jsonl + pdf_documents.jsonl and emits one flat record per
      retrievable unit - an HTML section, or a chunk of a PDF's text.
Inputs: data/corpus/*.jsonl (READ ONLY - Phase 1 output is frozen)
Outputs: RetrievalRecord objects, each carrying full provenance.
Why: retrieval quality is decided here, not in the vector index. The unit has
     to be small enough to be a precise answer and large enough to stand alone,
     and it must carry its citation from the very first step - a chunk that
     loses its URL can never be cited, no matter how well it ranks.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, asdict, field
from pathlib import Path

# Paragraph-ish split for PDF text: blank lines, or a newline before something
# that looks like a new clause. PDFs have no headings to split on.
_PARA_SPLIT = re.compile(r"\n\s*\n+")


@dataclass
class RetrievalRecord:
    """One retrievable unit. Every field here survives into the citation."""
    chunk_id: str            # stable across rebuilds: content-addressed
    source_url: str          # the canonical, citable URL
    raw_source_url: str      # exactly what was fetched (may carry a session token)
    title: str
    section_heading: str
    text: str
    language: str            # en | ar | unknown
    document_type: str       # html | pdf
    anchor: str | None = None      # deep-link fragment, when the section had one
    citation: str = ""             # source_url + #anchor when available
    pdf_page: int | None = None    # 1-based, when derivable
    chunk_index: int = 0           # position within its parent document
    section_level: int = 0         # h1..h6, for HTML
    word_count: int = 0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _chunk_id(source_url: str, kind: str, index: int, text: str) -> str:
    """Stable, content-addressed id.

    Content-addressed rather than positional, so that re-running the build after
    an unrelated corpus change does not invalidate every cached embedding.
    """
    h = hashlib.sha1(f"{source_url}|{kind}|{index}|{text[:200]}".encode()).hexdigest()
    return f"{kind}_{h[:16]}"


def load_html_records(path: Path, min_words: int = 8) -> list[RetrievalRecord]:
    """One record per heading-delimited section, as Phase 1 already split them."""
    out: list[RetrievalRecord] = []
    if not path.exists():
        return out

    for line in path.open(encoding="utf-8"):
        if not line.strip():
            continue
        doc = json.loads(line)
        url = doc.get("url", "")
        title = doc.get("title", "")
        lang = doc.get("language", "unknown")
        sections = doc.get("sections") or []

        # Tables hold the fee/rate numbers. Appending their markdown to the
        # section text keeps those numbers retrievable; without this a query
        # for "annual fee" can never match a page whose fees live in a table.
        table_md = "\n\n".join(t.get("markdown", "") for t in doc.get("tables", [])
                               if t.get("markdown"))

        if not sections:
            # A page with no headings still has body text worth retrieving.
            body = (doc.get("text") or "").strip()
            if body:
                sections = [{"heading": title, "level": 0, "text": body,
                             "anchor": None}]

        for i, sec in enumerate(sections):
            text = (sec.get("text") or "").strip()
            heading = (sec.get("heading") or "").strip()
            if table_md and i == 0:
                text = (text + "\n\n" + table_md).strip()
            if len(text.split()) < min_words and not table_md:
                continue
            # Prepend the heading: it is often the only place the product name
            # appears, and a section body that says "the annual fee is 350"
            # is unmatchable without "Gold Card" attached to it.
            embed_text = f"{title} - {heading}\n{text}" if heading else f"{title}\n{text}"
            anchor = sec.get("anchor")
            out.append(RetrievalRecord(
                chunk_id=_chunk_id(url, "html", i, text),
                source_url=url,
                raw_source_url=doc.get("source_url", url),
                title=title,
                section_heading=heading,
                text=embed_text,
                language=lang,
                document_type="html",
                anchor=anchor,
                citation=f"{url}#{anchor}" if anchor else url,
                chunk_index=i,
                section_level=int(sec.get("level") or 0),
                word_count=len(text.split()),
            ))
    return out


def chunk_pdf_text(text: str, target_words: int = 220,
                   overlap_words: int = 40) -> list[str]:
    """Split PDF text into overlapping, paragraph-aligned windows.

    Paragraph boundaries are preferred because a fee table row split down the
    middle retrieves badly. Overlap exists so a fact sitting on a boundary is
    not lost to both neighbours.
    """
    paras = [p.strip() for p in _PARA_SPLIT.split(text) if p.strip()]
    if not paras:
        paras = [l.strip() for l in text.splitlines() if l.strip()]
    if not paras:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_words = 0

    for para in paras:
        words = para.split()
        # A single oversized paragraph becomes its own hard-split windows.
        if len(words) > target_words * 2:
            if current:
                chunks.append("\n\n".join(current))
                current, current_words = [], 0
            step = target_words - overlap_words
            for start in range(0, len(words), step):
                window = words[start:start + target_words]
                if len(window) > 20 or start == 0:
                    chunks.append(" ".join(window))
            continue

        if current_words + len(words) > target_words and current:
            chunks.append("\n\n".join(current))
            # Carry the tail of the previous chunk forward as overlap.
            tail = "\n\n".join(current).split()[-overlap_words:]
            current = [" ".join(tail)] if tail else []
            current_words = len(tail)
        current.append(para)
        current_words += len(words)

    if current:
        chunks.append("\n\n".join(current))
    return [c for c in chunks if c.strip()]


def load_pdf_records(path: Path, min_words: int = 8,
                     target_words: int = 220) -> list[RetrievalRecord]:
    """One record per PDF text window, with a best-effort page number."""
    out: list[RetrievalRecord] = []
    if not path.exists():
        return out

    for line in path.open(encoding="utf-8"):
        if not line.strip():
            continue
        doc = json.loads(line)
        text = (doc.get("text") or "").strip()
        url = doc.get("url", "")
        title = doc.get("title", "") or url.rsplit("/", 1)[-1]
        n_pages = doc.get("pdf_pages") or 0

        if not text:
            # Scanned/unreadable attachment. Deliberately NOT indexed as
            # content - but recorded upstream so the agent can say the
            # document exists and could not be read.
            continue

        chunks = chunk_pdf_text(text, target_words=target_words)
        total_words = max(1, len(text.split()))
        running = 0
        for i, chunk in enumerate(chunks):
            if len(chunk.split()) < min_words:
                continue
            # Estimated page: proportional position through the document. It is
            # an approximation, and labelled as one, because pypi extraction
            # concatenates pages without reliable markers.
            page = None
            if n_pages:
                page = min(n_pages, 1 + int(running / total_words * n_pages))
            running += len(chunk.split())
            out.append(RetrievalRecord(
                chunk_id=_chunk_id(url, "pdf", i, chunk),
                source_url=url,
                raw_source_url=doc.get("source_url", url),
                title=title,
                section_heading=f"page ~{page}" if page else f"chunk {i + 1}",
                text=f"{title}\n{chunk}",
                language=doc.get("language", "unknown"),
                document_type="pdf",
                citation=url,
                pdf_page=page,
                chunk_index=i,
                word_count=len(chunk.split()),
                extra={"page_is_estimated": bool(page),
                       "attachment_kind": doc.get("attachment_kind")},
            ))
    return out


def load_all(corpus_dir: Path, min_words: int = 8,
             target_words: int = 220) -> list[RetrievalRecord]:
    """Load the whole frozen corpus as retrieval records. Never writes."""
    html = load_html_records(corpus_dir / "documents.jsonl", min_words=min_words)
    pdf = load_pdf_records(corpus_dir / "pdf_documents.jsonl",
                           min_words=min_words, target_words=target_words)
    return html + pdf
