"""
Real-HTTP fetcher, small-scale.

  * an identifying User-Agent names the bot and a contact address.
  * per-host rate limiting (thread-safe).
  * HTML-only: non-HTML content types are skipped, response size is capped.
  * timeouts + bounded retries with exponential backoff and jitter.
  * an on-disk cache so re-runs never re-hit a site.

It fetches public web pages only. It does not touch LinkedIn, Crunchbase, social
networks, or any login-walled source.
"""
from __future__ import annotations

import hashlib
import random
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

USER_AGENT = (
    "SMU-Competition-RecruitmentBot/1.0 "
    "(+https://lkygbpc.smu.edu.sg/; contact: competition-ops@smu.edu.sg)"
)
MAX_BYTES = 3_000_000            # never download more than ~3 MB from one page
HTML_TYPES = ("text/html", "application/xhtml+xml")


@dataclass
class FetchResult:
    url: str                     # final URL (after redirects)
    html: str | None
    ok: bool
    reason: str = ""


class RateLimiter:
    """Per-host minimum interval, thread-safe."""
    def __init__(self, default_delay: float = 1.0):
        self.default_delay = default_delay
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                nxt = self._next.get(host, 0.0)
                if now >= nxt:
                    self._next[host] = now + self.default_delay
                    return
                sleep_for = nxt - now
            time.sleep(min(sleep_for, self.default_delay))


class Fetcher:
    def __init__(self, *, delay: float = 1.0, timeout: float = 12.0, retries: int = 2,
                 cache_dir: str | Path = ".cache"):
        self.timeout = timeout
        self.retries = retries
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.rate = RateLimiter(delay)

    # -- cache -------------------------------------------------------------- #
    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:20] + ".html")

    # -- fetch -------------------------------------------------------------- #
    def get(self, url: str) -> FetchResult:
        cache = self._cache_path(url)
        if cache.exists():
            return FetchResult(url, cache.read_text("utf-8", "replace"), True, "cache")

        host = urlparse(url).netloc
        last_err = ""
        for attempt in range(self.retries + 1):
            self.rate.wait(host)
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    ctype = (resp.headers.get_content_type() or "").lower()
                    if ctype and ctype not in HTML_TYPES:
                        return FetchResult(resp.geturl(), None, False, f"non-html ({ctype})")
                    raw = resp.read(MAX_BYTES)
                    charset = resp.headers.get_content_charset() or "utf-8"
                    html = raw.decode(charset, "replace")
                    final = resp.geturl()
                cache.write_text(html, "utf-8")
                return FetchResult(final, html, True, "fetched")
            except HTTPError as e:
                last_err = f"http {e.code}"
                if e.code < 500 and e.code != 429:
                    return FetchResult(url, None, False, last_err)  # 4xx: don't retry
            except (URLError, TimeoutError) as e:
                last_err = f"network: {e}"
            if attempt < self.retries:
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
        return FetchResult(url, None, False, last_err or "failed")
