"""JavaScript rendering fallback, with manifest write-back.

What: re-fetches URLs with a real headless browser, overwrites their entry in
      the raw store, AND updates data/raw/manifest.jsonl so the rest of the
      pipeline sees the rendered state.
Inputs: URLs flagged `looks_unrendered` in the manifest, or --url.
Outputs: rendered HTML in the raw store + an updated manifest.
Why: many bank sites load product tables via XHR. `requests` then sees an empty
     shell and you would silently build a corpus of nothing.

Requires: pip install playwright && playwright install chromium
"""
from __future__ import annotations

import argparse
import json
import time

from bs4 import BeautifulSoup

import config
from crawl import extract_links, looks_unrendered
from fetcher import url_key, _now


def word_count(html: str) -> int:
    """Visible-text word count - the measure of whether rendering achieved anything."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return len(soup.get_text(" ", strip=True).split())


def render_urls(urls: list[str], wait_ms: int = 2500,
                wait_until: str = "domcontentloaded") -> dict[str, dict]:
    """Render each URL and return {url: {html, final_url, words_after}}.

    `wait_until` defaults to domcontentloaded, not networkidle. Sites with
    analytics beacons, chat widgets or polling never reach network idle, so
    that setting times out on every page and the whole run silently fails.
    """
    try:
        from playwright.sync_api import sync_playwright
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        raise SystemExit(f"cannot load playwright ({exc}); "
                         "pip install playwright && playwright install chromium") from exc

    results: dict[str, dict] = {}
    failures: list[tuple[str, str]] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=config.USER_AGENT, locale="en-US",
                                  viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        for i, url in enumerate(urls, 1):
            try:
                page.goto(url, timeout=int(config.REQUEST_TIMEOUT_SEC * 1000),
                          wait_until=wait_until)
                # Best-effort settle: give XHR-loaded content a chance to land,
                # but never fail the page just because the network stays chatty.
                try:
                    page.wait_for_load_state("networkidle", timeout=wait_ms)
                except Exception:                              # noqa: BLE001
                    page.wait_for_timeout(wait_ms)
                html = page.content()
                results[url] = {"html": html,
                                "final_url": config.normalise_url(page.url),
                                "words_after": word_count(html)}
                print(f"[render] {i}/{len(urls)} {results[url]['words_after']:>5}w  {url}")
            except Exception as exc:                           # noqa: BLE001
                failures.append((url, str(exc).splitlines()[0][:120]))
                print(f"[render] {i}/{len(urls)} FAILED {url}: {exc}".splitlines()[0])
            time.sleep(config.REQUEST_DELAY_SEC)
        browser.close()

    if failures:
        print(f"\n[render] {len(failures)} of {len(urls)} pages FAILED to render")
        for url, err in failures[:10]:
            print(f"[render]   {url} - {err}")
    return results


def write_back(results: dict[str, dict]) -> tuple[int, int]:
    """Write rendered HTML to the raw store and update the manifest in place.

    Without this the manifest keeps its crawl-time `looks_unrendered` flag
    forever, so audit.py reports the same count no matter how often you render.
    """
    pages_dir = config.RAW_DIR / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    for url, r in results.items():
        key = url_key(url)
        raw_path = pages_dir / f"{key}.html"
        raw_path.write_text(r["html"], encoding="utf-8")
        (pages_dir / f"{key}.meta.json").write_text(json.dumps({
            "url": url, "final_url": r["final_url"], "status": 200,
            "content_type": "text/html; charset=utf-8", "raw_path": str(raw_path),
            "fetched_at": _now(), "from_cache": False, "error": None,
            "kind": "html", "rendered": True,
        }, ensure_ascii=False), encoding="utf-8")
        r["raw_path"] = str(raw_path)

    if not config.CRAWL_MANIFEST.exists():
        print("[render] no manifest to update")
        return len(results), 0

    updated = 0
    lines_out = []
    for line in config.CRAWL_MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        r = results.get(rec["url"])
        if r:
            rec["words_before"] = rec.get("words_after", None)
            rec["raw_path"] = r["raw_path"]
            rec["final_url"] = r["final_url"]
            rec["fetched_at"] = _now()
            rec["status"] = 200
            rec["error"] = None
            rec["content_type"] = "text/html; charset=utf-8"
            rec["rendered"] = True
            rec["words_after"] = r["words_after"]
            # Re-run detection against the RENDERED html, and refresh the link
            # graph: JS-rendered navigation is invisible to the plain fetch.
            rec["looks_unrendered"] = looks_unrendered(r["html"])
            rec["outlinks"] = extract_links(r["html"], r["final_url"])
            updated += 1
        lines_out.append(json.dumps(rec, ensure_ascii=False))

    config.CRAWL_MANIFEST.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    return len(results), updated


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-fetch pages with a headless browser")
    ap.add_argument("--url", nargs="*", default=None)
    ap.add_argument("--all-unrendered", action="store_true",
                    help="render every URL the crawler flagged as client-rendered")
    ap.add_argument("--wait-until", default="domcontentloaded",
                    choices=["load", "domcontentloaded", "networkidle", "commit"])
    ap.add_argument("--wait-ms", type=int, default=2500)
    args = ap.parse_args()

    urls = list(args.url or [])
    before: dict[str, int] = {}
    if args.all_unrendered and config.CRAWL_MANIFEST.exists():
        for line in config.CRAWL_MANIFEST.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("looks_unrendered"):
                urls.append(rec["url"])
    if not urls:
        raise SystemExit("nothing to render (pass --url or --all-unrendered)")
    urls = sorted(set(urls))

    # Record the pre-render word count so the effect is measurable, not assumed.
    for line in (config.CRAWL_MANIFEST.read_text(encoding="utf-8").splitlines()
                 if config.CRAWL_MANIFEST.exists() else []):
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec["url"] in urls and rec.get("raw_path"):
            try:
                before[rec["url"]] = word_count(
                    open(rec["raw_path"], encoding="utf-8", errors="replace").read())
            except OSError:
                pass

    results = render_urls(urls, wait_ms=args.wait_ms, wait_until=args.wait_until)
    rendered, updated = write_back(results)

    gained = [(u, before.get(u, 0), r["words_after"]) for u, r in results.items()]
    improved = [g for g in gained if g[2] > g[1]]
    still_thin = [g for g in gained if g[2] < 80]

    print(f"\n[render] rendered {rendered}/{len(urls)}, manifest rows updated {updated}")
    if gained:
        print(f"[render] {len(improved)} pages gained text; "
              f"{len(still_thin)} still under 80 words")
        for u, b, a in sorted(gained, key=lambda g: g[1] - g[2])[:10]:
            print(f"[render]   {b:>5}w -> {a:>5}w  {u}")
    if rendered == 0:
        raise SystemExit("[render] every page failed to render - nothing was written. "
                         "Try --wait-until load, or raise BM_TIMEOUT.")
    print("[render] now re-run: python extract.py && python audit.py")


if __name__ == "__main__":
    main()
