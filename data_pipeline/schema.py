"""The corpus record schema.

What: defines the one document shape everything downstream depends on.
Inputs: none (pure definitions).
Outputs: `Document` dataclass + `to_jsonl_record`.
Why: the agent must cite sources and must know when information is stale or
     missing. That is only possible if provenance (url, fetched_at, section
     anchor) travels with the text from the very first stage, rather than being
     bolted on at retrieval time.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class Section:
    """A heading-delimited chunk of a page - the unit the agent cites."""
    heading: str
    level: int              # 1..6 from h1..h6
    text: str
    anchor: str | None = None   # id/name attr, so a citation can deep-link


@dataclass
class Table:
    """Tables carry the fee/rate/eligibility data. Kept structured, not flattened."""
    caption: str | None
    headers: list[str]
    rows: list[list[str]]
    markdown: str


@dataclass
class Document:
    url: str                       # canonical URL - the citation
    final_url: str
    title: str
    language: str                  # en | ar | unknown
    fetched_at: str                # freshness, for staleness checks
    content_hash: str              # change detection between crawls
    breadcrumbs: list[str] = field(default_factory=list)
    section_path: list[str] = field(default_factory=list)  # from URL structure
    meta_description: str | None = None
    text: str = ""                 # full readable text
    sections: list[Section] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    pdf_links: list[str] = field(default_factory=list)
    doc_type: str = "page"         # page | pdf
    word_count: int = 0

    def to_record(self) -> dict:
        return asdict(self)
