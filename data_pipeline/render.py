"""JavaScript rendering fallback (only needed if the site is client-rendered).

What: re-fetches URLs with a real headless browser and overwrites their entry
      in the raw store with the fully rendered DOM.
Inputs: URLs flagged `looks_unrendered` in data/raw/manifest.jsonl, or --url.
Outputs: same raw store layout as fetcher.py, so extract.py needs no changes.
Why: many modern bank sites load product tables via XHR. `requests` then sees
     an empty shell and you would silently build a corpus of nothing. Run
     crawl.py first, check its warning, and only run this if it fires.

Requires: pip install playwright && playwright install chromium
"""
from __future__ import annotations

import argparse
import json
import time

import config
from fetcher import url_key, _now


def render_urls(urls: list[str], wait_ms: int = 2500) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("playwright not installed: pip install playwright "
                         "&& playwright install chromium")

    pages_dir = config.RAW_DIR / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=config.USER_AGENT,
                                  locale="en-US", viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        for i, url in enumerate(urls, 1):
            try:
                page.goto(url, timeout=int(config.REQUEST_TIMEOUT_SEC * 1000),
                          wait_until="networkidle")
                page.wait_for_timeout(wait_ms)
                html = page.content()
            except Exception as exc:                      # noqa: BLE001
                print(f"[render] {url} failed: {exc}")
                continue

            key = url_key(url)
            (pages_dir / f"{key}.html").write_text(html, encoding="utf-8")
            (pages_dir / f"{key}.meta.json").write_text(json.dumps({
                "url": url, "final_url": config.normalise_url(page.url),
                "status": 200, "content_type": "text/html; charset=utf-8",
                "raw_path": str(pages_dir / f"{key}.html"),
                "fetched_at": _now(), "from_cache": False, "error": None,
                "rendered": True,
            }, ensure_ascii=False), encoding="utf-8")
            print(f"[render] {i}/{len(urls)} {url}")
            time.sleep(config.REQUEST_DELAY_SEC)
        browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-fetch pages with a headless browser")
    ap.add_argument("--url", nargs="*", default=None)
    ap.add_argument("--all-unrendered", action="store_true",
                    help="render every URL the crawler flagged as client-rendered")
    args = ap.parse_args()

    urls = list(args.url or [])
    if args.all_unrendered and config.CRAWL_MANIFEST.exists():
        for line in config.CRAWL_MANIFEST.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("looks_unrendered"):
                urls.append(rec["url"])
    if not urls:
        raise SystemExit("nothing to render (pass --url or --all-unrendered)")
    render_urls(sorted(set(urls)))


if __name__ == "__main__":
    main()
