"""
Polite HTTP client used by every data source.

- At most ~1 request per second per host (configurable).
- Retries with exponential backoff on network errors, 429 and 5xx.
  Honors the server's Retry-After header when it sends one.
- Optional on-disk cache with a time-to-live, so repeated runs don't
  re-download data that hasn't had time to change.
- Every request is logged (URL, status, time taken) for debugging.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

import requests

log = logging.getLogger("shark.http")

USER_AGENT = ("SharkTracker/0.1 (personal read-only research tool; "
              "public data only; ~1 req/s)")


class FetchError(Exception):
    """Raised when a request still fails after all retries."""


class PoliteClient:
    def __init__(self, cache_dir: Optional[str] = None, min_interval: float = 1.0,
                 max_retries: int = 4, timeout: float = 20.0):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_call: dict[str, float] = {}
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0}

    # -- cache -----------------------------------------------------------
    def _cache_path(self, url: str) -> Optional[Path]:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".json")

    def _cache_get(self, url: str, ttl: float) -> Any:
        p = self._cache_path(url)
        if not p or ttl <= 0 or not p.exists():
            return None
        if time.time() - p.stat().st_mtime > ttl:
            return None
        try:
            self.stats["cache_hits"] += 1
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None

    def _cache_put(self, url: str, data: Any) -> None:
        p = self._cache_path(url)
        if p:
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, p)

    # -- requests --------------------------------------------------------
    def _wait_turn(self, host: str) -> None:
        last = self._last_call.get(host, 0.0)
        gap = time.monotonic() - last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_call[host] = time.monotonic()

    def get_json(self, url: str, params: Optional[dict] = None, ttl: float = 0) -> Any:
        """GET a URL and return parsed JSON. `ttl` = seconds to reuse a cached copy."""
        full = url + ("?" + urlencode(params, doseq=True) if params else "")
        cached = self._cache_get(full, ttl)
        if cached is not None:
            return cached

        host = urlparse(full).netloc
        delay = 2.0
        last_err = ""
        for attempt in range(self.max_retries + 1):
            self._wait_turn(host)
            t0 = time.monotonic()
            try:
                r = self.session.get(full, timeout=self.timeout)
                self.stats["requests"] += 1
                took = time.monotonic() - t0
                log.debug("GET %s -> %s (%.2fs)", full, r.status_code, took)
                if r.status_code == 200:
                    data = r.json()
                    if ttl > 0:
                        self._cache_put(full, data)
                    return data
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code == 429 or r.status_code >= 500:
                    ra = r.headers.get("Retry-After")
                    wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else delay
                    log.warning("GET %s -> %s, retrying in %.0fs", full, r.status_code, wait)
                    time.sleep(min(wait, 60))
                    delay *= 2
                    continue
                break  # other 4xx: don't retry
            except (requests.RequestException, ValueError) as e:
                last_err = f"{type(e).__name__}: {e}"
                log.warning("GET %s failed (%s), retrying in %.0fs", full, last_err, delay)
                time.sleep(delay)
                delay *= 2
        self.stats["errors"] += 1
        raise FetchError(f"{full}: {last_err}")
