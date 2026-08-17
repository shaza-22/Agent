"""Polite, cached HTTP fetching.

What: one function, `fetch`, that gets a URL and writes the response body into
      the immutable raw store (content-addressed by URL hash).
Inputs: a URL, plus the shared `Fetcher` session holding robots rules + delay.
Outputs: a `FetchResult` and a file under data/raw/pages/<sha1>.html
Why: every network read in this project goes through here, so politeness,
     retries, caching and robots.txt compliance are enforced in one place and
     cannot be accidentally bypassed by a different script.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass, asdict
from pathlib import Path

import requests

import config


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content_type: str
    raw_path: str | None
    fetched_at: str
    from_cache: bool
    error: str | None = None

    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300


def url_key(url: str) -> str:
    """Stable filename for a URL. Content-addressed so re-runs are idempotent."""
    return hashlib.sha1(config.normalise_url(url).encode("utf-8")).hexdigest()


class Fetcher:
    """Session wrapper: robots.txt, rate limiting, retries, on-disk cache."""

    def __init__(self, *, use_cache: bool = True) -> None:
        config.ensure_dirs()
        self.pages_dir = config.RAW_DIR / "pages"
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        self.use_cache = use_cache
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en,ar;q=0.8",
        })
        self._last_request = 0.0
        self._robots = self._load_robots()

    # -- robots -------------------------------------------------------------
    def _load_robots(self):
        if not config.OBEY_ROBOTS:
            return None
        rp = robotparser.RobotFileParser()
        robots_url = f"{config.BASE_URL.rstrip('/')}/robots.txt"
        try:
            resp = self.session.get(robots_url, timeout=config.REQUEST_TIMEOUT_SEC)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
                (config.RAW_DIR / "robots.txt").write_text(resp.text, encoding="utf-8")
            else:
                rp.parse([])  # no robots.txt -> nothing disallowed
        except requests.RequestException as exc:
            print(f"[fetcher] robots.txt unreachable ({exc}); assuming allow-all")
            rp.parse([])
        return rp

    def allowed(self, url: str) -> bool:
        if self._robots is None:
            return True
        return self._robots.can_fetch(config.USER_AGENT, url)

    def sitemaps_from_robots(self) -> list[str]:
        robots_file = config.RAW_DIR / "robots.txt"
        if not robots_file.exists():
            return []
        return [
            line.split(":", 1)[1].strip()
            for line in robots_file.read_text(encoding="utf-8").splitlines()
            if line.lower().startswith("sitemap:")
        ]

    # -- fetching -----------------------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < config.REQUEST_DELAY_SEC:
            time.sleep(config.REQUEST_DELAY_SEC - elapsed)
        self._last_request = time.time()

    def fetch(self, url: str, *, binary: bool = False) -> FetchResult:
        url = config.normalise_url(url)
        key = url_key(url)
        suffix = ".bin" if binary else ".html"
        raw_path = self.pages_dir / f"{key}{suffix}"
        meta_path = self.pages_dir / f"{key}.meta.json"

        if self.use_cache and raw_path.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["from_cache"] = True
            return FetchResult(**meta)

        if not self.allowed(url):
            return FetchResult(url, url, 0, "", None, _now(), False,
                               error="blocked by robots.txt")

        last_error = None
        for attempt in range(config.MAX_RETRIES):
            self._throttle()
            try:
                resp = self.session.get(
                    url, timeout=config.REQUEST_TIMEOUT_SEC, allow_redirects=True
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_error = f"HTTP {resp.status_code}"
                    time.sleep(2 ** attempt * 2)  # 2s, 4s, 8s backoff
                    continue
                if binary:
                    raw_path.write_bytes(resp.content)
                else:
                    raw_path.write_text(resp.text, encoding="utf-8")
                result = FetchResult(
                    url=url,
                    final_url=config.normalise_url(resp.url),
                    status=resp.status_code,
                    content_type=resp.headers.get("Content-Type", ""),
                    raw_path=str(raw_path),
                    fetched_at=_now(),
                    from_cache=False,
                )
                meta = asdict(result)
                meta.pop("from_cache")
                meta["from_cache"] = False
                meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
                return result
            except requests.RequestException as exc:
                last_error = str(exc)
                time.sleep(2 ** attempt * 2)

        return FetchResult(url, url, 0, "", None, _now(), False, error=last_error)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
