"""Site-understanding + coverage report.

What: turns the crawl manifest and corpus into a readable report - what the
      site contains, how it is organised, which pages link to which, and where
      the data-gathering has gaps.
Inputs: data/raw/manifest.jsonl, data/corpus/documents.jsonl
Outputs: data/reports/site_report.md and data/reports/site_map.json
Why: section 6 of the brief ("Data & Website Understanding") is a deliverable
     in its own right, and the same numbers tell you when the crawl is actually
     finished. It also surfaces the failure modes - dead links, empty pages,
     client-rendered pages, Arabic-only sections - that would otherwise show up
     later as unexplained agent failures.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict

import config


def load_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def main() -> None:
    config.ensure_dirs()
    manifest = load_jsonl(config.CRAWL_MANIFEST)
    docs = load_jsonl(config.CORPUS_FILE)
    pdfs = load_jsonl(config.CORPUS_DIR / "pdf_documents.jsonl")

    if not manifest:
        raise SystemExit(f"no manifest at {config.CRAWL_MANIFEST}; run crawl.py first")

    statuses = Counter(r.get("status", 0) for r in manifest)
    errors = [r for r in manifest if r.get("error")]
    unrendered = [r for r in manifest if r.get("looks_unrendered")]
    langs = Counter(d["language"] for d in docs)

    # Top-level sections, from the first URL path segment after the language.
    sections = defaultdict(list)
    for d in docs:
        top = d["section_path"][0] if d["section_path"] else "(root)"
        sections[top].append(d)

    # Inbound link counts: which pages the site itself considers important.
    inbound = Counter()
    for r in manifest:
        for link in r.get("outlinks", []):
            inbound[link] += 1

    thin = [d for d in docs if d["word_count"] < 80]
    with_tables = [d for d in docs if d["tables"]]

    site_map = {
        "sections": {
            k: {
                "page_count": len(v),
                "sample_urls": [d["url"] for d in v[:8]],
                "sub_sections": sorted({
                    d["section_path"][1] for d in v if len(d["section_path"]) > 1
                }),
            }
            for k, v in sorted(sections.items(), key=lambda kv: -len(kv[1]))
        },
        "link_graph": {r["url"]: r.get("outlinks", []) for r in manifest if r.get("outlinks")},
    }
    (config.REPORT_DIR / "site_map.json").write_text(
        json.dumps(site_map, indent=2, ensure_ascii=False), encoding="utf-8")

    L = []
    L.append("# Banque Misr - site understanding & crawl coverage\n")
    L.append(f"- URLs visited: **{len(manifest)}**")
    L.append(f"- Documents extracted: **{len(docs)}**")
    L.append(f"- PDFs extracted: **{len(pdfs)}** "
             f"({sum(1 for p in pdfs if p.get('needs_ocr'))} need OCR)")
    L.append(f"- Pages with tables (fees/rates live here): **{len(with_tables)}**")
    L.append(f"- Languages: " + ", ".join(f"{k}={v}" for k, v in langs.most_common()))
    L.append("")

    L.append("## HTTP status breakdown\n")
    for status, count in sorted(statuses.items()):
        L.append(f"- `{status}`: {count}")
    if errors:
        L.append(f"\n**{len(errors)} fetch errors** (first 10):")
        for r in errors[:10]:
            L.append(f"- {r['url']} - {r['error']}")
    L.append("")

    L.append("## Content areas (what kinds of information exist)\n")
    L.append("| Section | Pages | Sub-sections |")
    L.append("| --- | --- | --- |")
    for name, info in site_map["sections"].items():
        subs = ", ".join(info["sub_sections"][:8]) or "-"
        L.append(f"| `{name}` | {info['page_count']} | {subs} |")
    L.append("")

    L.append("## Most-linked pages (the site's own view of what matters)\n")
    for url, count in inbound.most_common(20):
        L.append(f"- {count:>3} inbound - {url}")
    L.append("")

    L.append("## Gaps and risks\n")
    if unrendered:
        L.append(f"- **{len(unrendered)} pages looked client-rendered** - re-fetch with "
                 f"`python render.py --all-unrendered`, else their content is missing.")
    if thin:
        L.append(f"- **{len(thin)} pages have <80 words** - likely shells, redirects, "
                 f"or content behind a tab/accordion the parser missed.")
        for d in thin[:10]:
            L.append(f"  - {d['url']} ({d['word_count']} words)")
    if langs.get("unknown"):
        L.append(f"- **{langs['unknown']} pages have an undetermined language** - "
                 f"check `config.detect_language` against the real URL scheme.")
    if not pdfs:
        L.append("- **No PDFs extracted** - if the site publishes tariff/rate PDFs, "
                 "the agent cannot answer fee questions. Run `pdf_ingest.py`.")
    L.append("")

    L.append("## Task feasibility check\n")
    L.append("Before building the agent, confirm the corpus can answer the brief's "
             "example tasks. Each line should return real pages:\n")
    L.append("```bash")
    for term in ("credit card", "eligibility", "annual fee", "account opening",
                 "required documents", "interest rate"):
        L.append(f'grep -ic "{term}" data/corpus/documents.jsonl   # "{term}"')
    L.append("```")

    out = config.REPORT_DIR / "site_report.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"[audit] wrote {out} and {config.REPORT_DIR / 'site_map.json'}")
    print(f"[audit] {len(manifest)} URLs, {len(docs)} docs, {len(pdfs)} PDFs")


if __name__ == "__main__":
    main()
