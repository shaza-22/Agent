"""Evidence tool: what does the raw store actually contain right now?

What: compares the manifest against the files on disk for specific URLs (or the
      whole corpus) and reports mtime, size, word count, rendered flag and any
      inconsistency between them.
Inputs: --url <urls...> | --all-unrendered | --sample N
Outputs: a table on stdout; --json for machine-readable output.
Why: when a pipeline stage appears to have done nothing, the first question is
     whether the bytes on disk changed. Guessing wastes hours; this answers it
     in one command.

Examples:
    python diagnose.py --all-unrendered
    python diagnose.py --url https://www.banquemisr.com/en/tariff
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from bs4 import BeautifulSoup

import config
import soft404
from fetcher import url_key


def word_count(path: Path) -> int:
    try:
        html = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return -1
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return len(soup.get_text(" ", strip=True).split())


def inspect(rec: dict) -> dict:
    url = rec["url"]
    pages_dir = config.RAW_DIR / "pages"
    key = url_key(url)
    manifest_path = Path(rec["raw_path"]) if rec.get("raw_path") else None
    expected = pages_dir / f"{key}.html"
    meta_file = pages_dir / f"{key}.meta.json"
    meta = {}
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except ValueError:
            meta = {}

    on_disk = manifest_path if (manifest_path and manifest_path.exists()) else (
        expected if expected.exists() else None)

    out = {
        "url": url,
        "manifest_kind": rec.get("kind", "?"),
        "manifest_status": rec.get("status"),
        "manifest_looks_unrendered": bool(rec.get("looks_unrendered")),
        "manifest_says_rendered": bool(rec.get("rendered")),
        "meta_says_rendered": bool(meta.get("rendered")),
        "file": str(on_disk) if on_disk else None,
        "size_bytes": on_disk.stat().st_size if on_disk else 0,
        "mtime": (time.strftime("%Y-%m-%d %H:%M:%S",
                                time.localtime(on_disk.stat().st_mtime))
                  if on_disk else None),
        "words_on_disk": word_count(on_disk) if on_disk else -1,
        "words_after_render": rec.get("words_after"),
    }

    problems = []
    if not on_disk:
        problems.append("NO FILE on disk for this URL")
    if manifest_path and not manifest_path.exists():
        problems.append(f"manifest raw_path missing: {manifest_path}")
    if out["meta_says_rendered"] and not out["manifest_says_rendered"]:
        problems.append("RENDERED ON DISK BUT MANIFEST NOT UPDATED "
                        "(audit.py will keep reporting it as client-rendered)")
    if out["meta_says_rendered"] and out["manifest_looks_unrendered"] \
            and out["words_on_disk"] >= 400:
        problems.append("stale looks_unrendered flag: page has text now")
    if out["words_on_disk"] == 0:
        problems.append("file has zero visible words")
    out["problems"] = problems
    return out


def fingerprint(manifest: list[dict], limit: int = 0) -> None:
    """Group pages by their visible text and show what the big clusters are.

    Many unrelated URLs sharing one byte-identical body is the signature of a
    template being served in place of content - a soft 404, a challenge page,
    or a consent wall. This prints the shared text so you can see which.
    """
    from collections import defaultdict
    groups: dict[str, list[str]] = defaultdict(list)
    samples: dict[str, str] = {}
    sizes: dict[str, int] = {}

    rows = [r for r in manifest if r.get("raw_path")]
    if limit:
        rows = rows[:limit]

    for rec in rows:
        try:
            html = Path(rec["raw_path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        h = soft404.text_hash(html)
        groups[h].append(rec["url"])
        if h not in samples:
            samples[h] = soft404.visible_text(html)
            sizes[h] = len(html.encode("utf-8"))

    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    print(f"Grouped {sum(len(v) for v in groups.values())} pages into "
          f"{len(groups)} distinct bodies.\n")

    for h, urls in ranked[:5]:
        if len(urls) == 1:
            continue
        text = samples[h]
        print("=" * 78)
        print(f"{len(urls)} URLs share ONE identical body "
              f"({len(text.split())} words, {sizes[h]} bytes, hash {h})")
        flags = soft404.classify_interstitial(text)
        if flags:
            print(f"  INTERSTITIAL DETECTED: {', '.join(flags)}")
        if soft404.looks_like_not_found(text):
            print("  NOT-FOUND WORDING DETECTED -> these URLs do not exist "
                  "(soft 404: HTTP 200 with a 'page not found' template)")
        if not flags and not soft404.looks_like_not_found(text):
            print("  No known signature. Read the text below and judge:")
        print("\n  --- visible text of the shared page ---")
        print("  " + (text[:800] or "(no visible text at all)"))
        print("\n  --- example URLs ---")
        for u in urls[:8]:
            print(f"    {u}")
        if len(urls) > 8:
            print(f"    ... and {len(urls) - 8} more")
        print()

    unique = sum(1 for _, v in groups.items() if len(v) == 1)
    print(f"{unique} pages have a body unique to themselves (these look real).")


def main() -> None:
    ap = argparse.ArgumentParser(description="Inspect raw store vs manifest")
    ap.add_argument("--url", nargs="*", default=[])
    ap.add_argument("--all-unrendered", action="store_true")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fingerprint", action="store_true",
                    help="group all pages by body text to find templates served "
                         "in place of content (soft 404s, challenge/consent walls)")
    args = ap.parse_args()

    if not config.CRAWL_MANIFEST.exists():
        raise SystemExit(f"no manifest at {config.CRAWL_MANIFEST}")
    manifest = [json.loads(l) for l in
                config.CRAWL_MANIFEST.read_text(encoding="utf-8").splitlines()
                if l.strip()]

    if args.fingerprint:
        fingerprint(manifest)
        return

    wanted = {config.normalise_url(u) for u in args.url}
    rows = [r for r in manifest if r["url"] in wanted] if wanted else []
    if args.all_unrendered:
        rows += [r for r in manifest if r.get("looks_unrendered")]
    if args.sample:
        rows += manifest[: args.sample]
    if not rows:
        raise SystemExit("no matching URLs (pass --url, --all-unrendered or --sample)")

    seen, unique = set(), []
    for r in rows:
        if r["url"] not in seen:
            seen.add(r["url"])
            unique.append(r)

    results = [inspect(r) for r in unique]

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return

    print(f"{'words':>7} {'size':>9} {'mtime':>19}  {'rendered':<18} url")
    print("-" * 110)
    for r in results:
        rendered = ("disk=yes" if r["meta_says_rendered"] else "disk=no ") + \
                   ("/mf=yes" if r["manifest_says_rendered"] else "/mf=no ")
        print(f"{r['words_on_disk']:>7} {r['size_bytes']:>9} "
              f"{r['mtime'] or '-':>19}  {rendered:<18} {r['url']}")

    problems = [r for r in results if r["problems"]]
    if problems:
        print(f"\n{len(problems)} of {len(results)} URLs have problems:\n")
        for r in problems:
            print(f"  {r['url']}")
            for p in r["problems"]:
                print(f"    - {p}")
    else:
        print(f"\nAll {len(results)} URLs consistent between manifest and disk.")


if __name__ == "__main__":
    main()
