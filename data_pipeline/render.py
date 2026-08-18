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
import soft404
from crawl import extract_links, looks_unrendered
from fetcher import url_key, _now

# Cookie/consent banners that sit on top of the page. Dismissed before capture
# so the overlay does not suppress or obscure the real content.
CONSENT_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "#onetrust-reject-all-handler",
    "button#accept-cookies",
    "button[aria-label*='Accept' i]",
    "button:has-text('Accept All')",
    "button:has-text('Accept all')",
    "button:has-text('Accept')",
    "button:has-text('I agree')",
    "button:has-text('Agree')",
    "a:has-text('Accept Cookies')",
    ".cookie-consent button",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
]


def dismiss_consent(page) -> str | None:
    """Click the first consent button that is actually present. Returns which."""
    for sel in CONSENT_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible():
                el.click(timeout=2000)
                page.wait_for_timeout(500)
                return sel
        except Exception:                                  # noqa: BLE001
            continue
    return None


def word_count(html: str) -> int:
    """Visible-text word count - the measure of whether rendering achieved anything."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return len(soup.get_text(" ", strip=True).split())


def render_urls(urls: list[str], wait_ms: int = 2500,
                wait_until: str = "domcontentloaded",
                headless: bool = True, accept_consent: bool = True) -> dict[str, dict]:
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
    blocked_urls: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        # A plain headless context is trivially fingerprintable. These are the
        # cheap, honest adjustments - a real UA string, a real viewport, real
        # Accept-Language - not evasion. If the site still blocks, that is a
        # deliberate signal to respect rather than defeat.
        ctx = browser.new_context(
            user_agent=config.USER_AGENT, locale="en-US",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9,ar;q=0.8"},
        )
        page = ctx.new_page()
        consent_hits: dict[str, int] = {}
        consecutive_blocks = 0
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
                if accept_consent:
                    hit = dismiss_consent(page)
                    if hit:
                        consent_hits[hit] = consent_hits.get(hit, 0) + 1
                        try:
                            page.wait_for_load_state("networkidle", timeout=wait_ms)
                        except Exception:                  # noqa: BLE001
                            page.wait_for_timeout(500)
                html = page.content()
                results[url] = {"html": html,
                                "final_url": config.normalise_url(page.url),
                                "words_after": word_count(html)}
                if soft404.looks_blocked(html):
                    consecutive_blocks += 1
                    blocked_urls.append(url)
                    results.pop(url, None)   # never store a block page as content
                else:
                    consecutive_blocks = 0
                flags = soft404.classify_interstitial(html)
                note = f"  [{','.join(flags)}]" if flags else ""
                print(f"[render] {i}/{len(urls)} {results[url]['words_after']:>5}w  "
                      f"{url}{note}")
            except Exception as exc:                           # noqa: BLE001
                failures.append((url, str(exc).splitlines()[0][:120]))
                print(f"[render] {i}/{len(urls)} FAILED {url}: {exc}".splitlines()[0])
            time.sleep(config.RENDER_DELAY_SEC)

            # Stop early rather than grinding through a block.
            if consecutive_blocks >= config.MAX_CONSECUTIVE_BLOCKS:
                print(f"\n[render] STOPPING: {consecutive_blocks} consecutive block "
                      f"pages. The site is refusing this browser. Nothing further "
                      f"would be usable. Run: python blockcheck.py")
                break
        browser.close()

    if blocked_urls:
        print(f"\n[render] {len(blocked_urls)} pages returned a BLOCK page and were "
              f"NOT stored. This is not a rendering problem - the site refused "
              f"the request. Run: python blockcheck.py")
    if consent_hits:
        print(f"[render] dismissed consent banners: {consent_hits}")
    if failures:
        print(f"\n[render] {len(failures)} of {len(urls)} pages FAILED to render")
        for url, err in failures[:10]:
            print(f"[render]   {url} - {err}")

    # The check that catches "every page came back identical" immediately,
    # instead of it surfacing three stages later as a word count that never moves.
    if len(results) > 1:
        hashes = {soft404.text_hash(r["html"]) for r in results.values()}
        if len(hashes) == 1:
            sample = soft404.visible_text(next(iter(results.values()))["html"])
            flags = soft404.classify_interstitial(sample)
            print(f"\n[render] WARNING: all {len(results)} pages rendered to ONE "
                  f"identical body ({len(sample.split())} words).")
            if flags:
                print(f"[render] signature: {', '.join(flags)}")
            if soft404.looks_like_not_found(sample):
                print("[render] the shared page says 'not found' - these URLs do "
                      "not exist on the site (soft 404). Fix the URL list, not "
                      "the renderer.")
            print(f"[render] shared text: {sample[:300]}")
        elif len(hashes) < len(results) / 2:
            print(f"\n[render] NOTE: {len(results)} pages produced only "
                  f"{len(hashes)} distinct bodies - check for template pages "
                  f"with: python diagnose.py --fingerprint")
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
    ap.add_argument("--headed", action="store_true",
                    help="run a visible browser (some bot checks treat headless "
                         "differently; also lets you watch what happens)")
    ap.add_argument("--no-consent", action="store_true",
                    help="do not try to dismiss cookie banners")
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

    results = render_urls(urls, wait_ms=args.wait_ms, wait_until=args.wait_until,
                          headless=not args.headed,
                          accept_consent=not args.no_consent)
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
