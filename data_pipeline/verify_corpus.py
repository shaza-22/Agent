"""Trust gate: prove the corpus is complete, grounded and citable.

What: audits the collected corpus against explicit trust rules and fails loudly.
Inputs: data/raw/manifest.jsonl, data/corpus/documents.jsonl, pdf_documents.jsonl
Outputs: data/reports/verification.md + verification.json; exit code 1 on FAIL.
Why: "trustworthy" has to mean something checkable. An agent that cites a URL
     which 404s, or answers from a page whose text never loaded, is worse than
     one that says "I don't know". This is the gate that catches that before
     the agent is ever built - and it is the evidence for the brief's
     Validation/Verification requirement.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone

import config

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


class Checks:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def add(self, name: str, status: str, detail: str, examples: list | None = None) -> None:
        self.results.append({"check": name, "status": status, "detail": detail,
                             "examples": (examples or [])[:10]})

    def worst(self) -> str:
        statuses = {r["status"] for r in self.results}
        return FAIL if FAIL in statuses else (WARN if WARN in statuses else PASS)


def load_jsonl(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def parse_ts(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def run_checks(manifest: list[dict], docs: list[dict], pdfs: list[dict],
               max_age_days: int) -> Checks:
    c = Checks()
    # Compare canonical forms on both sides. The manifest holds URLs exactly as
    # they were requested (mixed case, session tokens); documents hold the
    # canonical form. Comparing them raw reports every document as unresolvable
    # while nothing is actually wrong.
    ok_urls = set()
    for r in manifest:
        if 200 <= r.get("status", 0) < 300 and not r.get("blocked"):
            ok_urls.add(config.normalise_url(r["url"]))
            if r.get("final_url"):
                ok_urls.add(config.normalise_url(r["final_url"]))

    # --- 1. The corpus exists at all ---------------------------------------
    if not docs:
        c.add("corpus non-empty", FAIL,
              "No documents extracted. Run discover.py -> crawl.py -> extract.py.")
        return c
    c.add("corpus non-empty", PASS, f"{len(docs)} documents, {len(pdfs)} PDFs")

    # --- 2. Provenance completeness ----------------------------------------
    required = ("url", "title", "fetched_at", "content_hash", "language")
    missing = [d.get("url", "?") for d in docs
               if any(not d.get(f) for f in required)]
    c.add("every record carries provenance",
          FAIL if missing else PASS,
          f"{len(missing)} of {len(docs)} records missing one of {required}",
          missing)

    # --- 3. Citations resolve to a page that actually returned 2xx ---------
    unresolvable = [d["url"] for d in docs
                    if config.normalise_url(d["url"]) not in ok_urls
                    and config.normalise_url(d.get("source_url") or d["url"])
                    not in ok_urls]
    c.add("every citation URL fetched successfully",
          FAIL if unresolvable else PASS,
          f"{len(unresolvable)} documents cite a URL with no 2xx crawl record",
          unresolvable)

    # --- 4. Grounding: no empty or shell pages -----------------------------
    empty = [d["url"] for d in docs if d.get("word_count", 0) < 20]
    c.add("no empty/shell documents",
          FAIL if empty else PASS,
          f"{len(empty)} documents under 20 words (JS-rendered? run render.py)",
          empty)

    # --- 5. Duplicate content ----------------------------------------------
    by_hash = defaultdict(list)
    for d in docs:
        by_hash[d["content_hash"]].append(d["url"])
    dupes = {h: u for h, u in by_hash.items() if len(u) > 1}
    c.add("no duplicate content",
          WARN if dupes else PASS,
          f"{len(dupes)} content hashes shared by more than one URL "
          f"(duplicates compete in retrieval and skew ranking)",
          [f"{len(u)}x {u[0]}" for u in dupes.values()])

    # --- 6. Language tagging ------------------------------------------------
    langs = Counter(d["language"] for d in docs)
    unknown = [d["url"] for d in docs if d["language"] == "unknown"]
    c.add("language tagged on every document",
          WARN if unknown else PASS,
          f"{dict(langs)}; {len(unknown)} untagged", unknown)

    # --- 7. Sections are citable -------------------------------------------
    with_sections = [d for d in docs if d.get("sections")]
    anchored = sum(1 for d in docs for s in d.get("sections", []) if s.get("anchor"))
    total_sections = sum(len(d.get("sections", [])) for d in docs)
    ratio = len(with_sections) / len(docs)
    c.add("documents split into citable sections",
          PASS if ratio > 0.7 else WARN,
          f"{len(with_sections)}/{len(docs)} documents have sections; "
          f"{total_sections} sections total, {anchored} with deep-link anchors")

    # --- 8. Tables are well formed -----------------------------------------
    bad_tables = []
    n_tables = 0
    for d in docs:
        for t in d.get("tables", []):
            n_tables += 1
            widths = {len(r) for r in t.get("rows", [])}
            if t.get("headers") and widths and max(widths) > len(t["headers"]):
                bad_tables.append(f"{d['url']} (row wider than header)")
            if not t.get("rows"):
                bad_tables.append(f"{d['url']} (empty table)")
    c.add("tables well formed",
          WARN if bad_tables else PASS,
          f"{n_tables} tables extracted, {len(bad_tables)} malformed. "
          f"Tables carry the fees/rates - malformed ones produce wrong answers.",
          bad_tables)
    if n_tables == 0:
        c.add("fee/rate tables present", WARN,
              "No tables at all. Either the site puts rates in PDFs only, or the "
              "extractor is missing tab/accordion content. Verify by hand.")

    # --- 9. PDF coverage ----------------------------------------------------
    # Both sides canonicalised, or the sets never intersect and every linked
    # PDF looks missing while every fetched one looks unlinked.
    linked_pdfs = {config.normalise_url(u)
                   for d in docs for u in d.get("pdf_links", [])}
    got_pdfs = {config.normalise_url(p["url"]) for p in pdfs}
    got_pdfs |= {config.normalise_url(p["source_url"]) for p in pdfs
                 if p.get("source_url")}
    unfetched = sorted(linked_pdfs - got_pdfs)
    extra = sorted(got_pdfs - linked_pdfs)
    c.add("linked PDFs were fetched",
          WARN if unfetched else PASS,
          f"{len(linked_pdfs)} linked from pages, {len(pdfs)} attachment "
          f"documents held, {len(linked_pdfs & got_pdfs)} matched; "
          f"{len(unfetched)} linked-but-not-fetched (run pdf_ingest.py), "
          f"{len(extra)} fetched-but-not-linked (queued from the crawl, "
          f"or linked from a page that was blocked/dropped)",
          unfetched)
    ocr = [p["url"] for p in pdfs if p.get("needs_ocr")]
    if ocr:
        c.add("PDFs are machine-readable", WARN,
              f"{len(ocr)} PDFs are scanned images with no extractable text. "
              f"The agent must report these as unreadable, never guess them.", ocr)

    # --- 10. Freshness ------------------------------------------------------
    ages = [parse_ts(d["fetched_at"]) for d in docs]
    ages = [a for a in ages if a]
    if ages:
        oldest = min(ages)
        age_days = (datetime.now(timezone.utc) - oldest).days
        c.add("corpus is fresh",
              WARN if age_days > max_age_days else PASS,
              f"oldest document fetched {age_days} days ago "
              f"(threshold {max_age_days}); rates and offers change")

    # --- 11. Crawl health ---------------------------------------------------
    errs = [r for r in manifest if r.get("error")]
    unrendered = [r["url"] for r in manifest if r.get("looks_unrendered")]
    c.add("crawl completed cleanly",
          WARN if (errs or unrendered) else PASS,
          f"{len(errs)} fetch errors, {len(unrendered)} client-rendered pages "
          f"not yet re-fetched with render.py",
          [r["url"] for r in errs] + unrendered)

    # --- 12. Task feasibility ----------------------------------------------
    # The brief's own example tasks must be answerable from the corpus.
    blob = "\n".join((d.get("text") or "").lower() for d in docs)
    blob += "\n".join((p.get("text") or "").lower() for p in pdfs)
    terms = ["credit card", "eligibility", "fee", "account", "document",
             "interest rate", "loan"]
    absent = [t for t in terms if t not in blob]
    c.add("brief's example tasks are answerable",
          FAIL if len(absent) > 2 else (WARN if absent else PASS),
          f"terms with zero corpus coverage: {absent or 'none'}. "
          f"No retrieval strategy can recover a term the corpus never gathered.",
          absent)
    return c


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify the collected corpus is trustworthy")
    ap.add_argument("--max-age-days", type=int, default=30)
    ap.add_argument("--strict", action="store_true",
                    help="treat WARN as failure (use in CI)")
    args = ap.parse_args()

    config.ensure_dirs()
    manifest = load_jsonl(config.CRAWL_MANIFEST)
    docs = load_jsonl(config.CORPUS_FILE)
    pdfs = load_jsonl(config.CORPUS_DIR / "pdf_documents.jsonl")

    checks = run_checks(manifest, docs, pdfs, args.max_age_days)
    overall = checks.worst()

    icon = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL"}
    lines = ["# Corpus verification report\n",
             f"**Overall: {overall}**  -  {len(docs)} documents, {len(pdfs)} PDFs, "
             f"{len(manifest)} URLs visited\n",
             "| Check | Status | Detail |", "| --- | --- | --- |"]
    for r in checks.results:
        lines.append(f"| {r['check']} | **{icon[r['status']]}** | {r['detail']} |")
    for r in checks.results:
        if r["status"] != PASS and r["examples"]:
            lines.append(f"\n### {r['check']} - examples\n")
            lines += [f"- {e}" for e in r["examples"]]

    out_md = config.REPORT_DIR / "verification.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")
    (config.REPORT_DIR / "verification.json").write_text(
        json.dumps({"overall": overall, "checks": checks.results}, indent=2,
                   ensure_ascii=False), encoding="utf-8")

    for r in checks.results:
        print(f"[{icon[r['status']]}] {r['check']}: {r['detail']}")
    print(f"\n[verify] overall: {overall} -> {out_md}")

    if overall == FAIL or (args.strict and overall == WARN):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
