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


def main() -> None:
    ap = argparse.ArgumentParser(description="Inspect raw store vs manifest")
    ap.add_argument("--url", nargs="*", default=[])
    ap.add_argument("--all-unrendered", action="store_true")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not config.CRAWL_MANIFEST.exists():
        raise SystemExit(f"no manifest at {config.CRAWL_MANIFEST}")
    manifest = [json.loads(l) for l in
                config.CRAWL_MANIFEST.read_text(encoding="utf-8").splitlines()
                if l.strip()]

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
