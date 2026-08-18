"""Determine WHY the site is blocking, using as few requests as possible.

What: runs a small, spaced set of probes that separate the plausible causes -
      rate/volume, IP-level block, or headless-browser fingerprinting.
Inputs: --url <a known-good page>  (defaults to the site root)
Outputs: a verdict plus the specific setting to change.
Why: the fixes are mutually exclusive. Slowing down does nothing if the block
     is fingerprint-based; changing browser flags does nothing if you are
     simply asking for too much, too fast. Guessing wrong costs hours and more
     blocked requests.

This deliberately makes very few requests, widely spaced. It is a diagnostic on
someone else's production system: the polite move is to learn the answer with
the minimum possible traffic, and to stop as soon as the answer is clear.
"""
from __future__ import annotations

import argparse
import time

import config
import soft404


def probe_requests(url: str, label: str) -> tuple[str, str]:
    """One plain HTTP GET. Returns (classification, first 200 chars of text)."""
    import requests
    try:
        resp = requests.get(url, timeout=config.REQUEST_TIMEOUT_SEC, headers={
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        })
    except Exception as exc:                                   # noqa: BLE001
        return "error", str(exc)[:200]
    cls = soft404.classify_response(resp.text)
    print(f"[blockcheck] {label:<28} HTTP {resp.status_code} -> {cls}")
    return cls, soft404.visible_text(resp.text)[:200]


def probe_playwright(url: str, label: str, headless: bool) -> tuple[str, str]:
    """One headless (or headed) browser navigation."""
    try:
        from playwright.sync_api import sync_playwright
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        return "unavailable", f"playwright not installed ({exc})"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            ctx = browser.new_context(
                user_agent=config.USER_AGENT, locale="en-US",
                viewport={"width": 1440, "height": 900},
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9,ar;q=0.8"})
            page = ctx.new_page()
            page.goto(url, timeout=int(config.REQUEST_TIMEOUT_SEC * 1000),
                      wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            html = page.content()
            browser.close()
    except Exception as exc:                                   # noqa: BLE001
        return "error", str(exc).splitlines()[0][:200]

    cls = soft404.classify_response(html)
    print(f"[blockcheck] {label:<28} -> {cls}")
    return cls, soft404.visible_text(html)[:200]


def main() -> None:
    ap = argparse.ArgumentParser(description="Diagnose why the site is blocking")
    ap.add_argument("--url", default=config.BASE_URL,
                    help="a page known to exist (default: site root)")
    ap.add_argument("--gap", type=int, default=30,
                    help="seconds between probes (default 30)")
    args = ap.parse_args()

    url = config.normalise_url(args.url)
    print(f"[blockcheck] target: {url}")
    print(f"[blockcheck] 4 probes, {args.gap}s apart. This is intentionally slow.\n")

    results: dict[str, str] = {}

    # 1. Plain GET. If this is blocked too, the block is not about the browser.
    results["requests"], sample = probe_requests(url, "plain requests GET")
    time.sleep(args.gap)

    # 2. Repeat after a pause. If probe 1 was blocked and this one is not, the
    #    block is transient - a rate limit that decays.
    results["requests_after_pause"], _ = probe_requests(url, f"plain GET after {args.gap}s")
    time.sleep(args.gap)

    # 3. Headless browser, same pacing. Differs from (2) only in the client.
    results["headless"], _ = probe_playwright(url, "playwright headless", True)
    time.sleep(args.gap)

    # 4. Headed browser. Differs from (3) only in headless mode.
    results["headed"], _ = probe_playwright(url, "playwright headed", False)

    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)
    for k, v in results.items():
        print(f"  {k:<22} {v}")

    req_ok = results["requests"] == "content" or results["requests_after_pause"] == "content"
    headless_blocked = results["headless"] == "blocked"
    headed_ok = results["headed"] == "content"
    both_blocked = results["requests"] == "blocked" and results["requests_after_pause"] == "blocked"

    print()
    if results["requests"] == "blocked" and results["requests_after_pause"] == "content":
        print("RATE LIMIT that decays with time.")
        print("  The same request succeeded after a pause, so nothing about this")
        print("  client is banned - you were simply going too fast.")
        print("  Fix:  export BM_DELAY=5  BM_JITTER=3  BM_RENDER_DELAY=10")
        print("  Then re-run in smaller batches and stop at the first block.")
    elif req_ok and headless_blocked and headed_ok:
        print("HEADLESS-BROWSER FINGERPRINTING.")
        print("  Plain GETs and a headed browser both pass; only headless is")
        print("  refused. Fix:  python render.py --all-unrendered --headed")
        print("  Keep BM_RENDER_DELAY high regardless - renders are heavy.")
    elif req_ok and headless_blocked and not headed_ok:
        print("BROWSER TRAFFIC IS BLOCKED, plain GETs are not.")
        print("  Both browser modes are refused. Do not escalate to stealth")
        print("  tooling on a production bank site. Prefer: capture content via")
        print("  plain GETs, and get the JS-rendered pages another way (their")
        print("  underlying JSON endpoints, or ask the bank for the data).")
    elif both_blocked:
        print("IP-LEVEL / PERSISTENT BLOCK.")
        print("  Even a single plain GET after a pause is refused, so this is not")
        print("  about pacing or the browser. Stop crawling. Wait several hours.")
        print("  If it persists, the block is deliberate: contact the bank and")
        print("  ask for permission or a data export. Do not try to evade it.")
    else:
        print("NO BLOCK REPRODUCED right now.")
        print("  The earlier block was probably transient. Re-run the crawl with")
        print("  BM_DELAY=5 BM_JITTER=3 and watch for the first block - the")
        print("  fetcher now stops after 3 consecutive ones.")

    print("\nBlock-page text seen (if any):")
    print(f"  {sample[:200]}")


if __name__ == "__main__":
    main()
