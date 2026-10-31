"""
Entry point for the scheduled refresh (GitHub Actions).

1. Ask the website for the user's settings, match decisions and saved state.
2. Run one refresh.
3. Send the new data + state back (gzip-compressed), then a short report.
   If the refresh fails, only the report is sent, so the website keeps the
   last good data and shows the failure message.

Needs two environment variables:
  SITE_URL    e.g. https://shark-tracker.<name>.workers.dev
  INGEST_KEY  shared secret (GitHub secret + Cloudflare secret)
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import sys
import time
import traceback
from typing import Any

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from collector.pipeline import run  # noqa: E402

log = logging.getLogger("shark.remote")


class Site:
    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.h = {"Authorization": "Bearer " + key, "User-Agent": "shark-tracker-collector"}

    def load(self) -> tuple[dict, dict, dict]:
        inp = requests.get(self.url + "/api/collector/input", headers=self.h, timeout=60)
        inp.raise_for_status()
        st = requests.get(self.url + "/api/collector/state", headers=self.h, timeout=120)
        st.raise_for_status()
        j = inp.json()
        return j.get("settings") or {}, j.get("decisions") or {}, st.json() or {}

    def _put(self, path: str, obj: Any) -> int:
        raw = gzip.compress(json.dumps(obj, separators=(",", ":")).encode(), 6)
        r = requests.put(self.url + path, data=raw, timeout=180,
                         headers={**self.h, "Content-Type": "application/octet-stream"})
        r.raise_for_status()
        return len(raw)

    def save(self, data: dict, state: dict) -> None:
        sb = self._put("/api/collector/state", state)
        db = self._put("/api/collector/data", data)
        log.info("uploaded state %.2f MB, data %.2f MB (compressed)", sb / 1e6, db / 1e6)

    def report(self, ok: bool, seconds: int, trigger: str, message: str) -> None:
        r = requests.post(self.url + "/api/collector/report", timeout=60, headers=self.h,
                          json={"ok": ok, "seconds": seconds, "trigger": trigger, "message": message})
        r.raise_for_status()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("shark.http").setLevel(logging.WARNING)
    site = Site(os.environ["SITE_URL"], os.environ["INGEST_KEY"])
    trigger = os.environ.get("TRIGGER", "schedule")
    t0 = time.time()
    settings, decisions, state = site.load()
    try:
        data, state = run(settings, decisions, state)
        site.save(data, state)
        site.report(True, round(time.time() - t0), trigger, data["status"]["polymarket"]["message"])
        return 0
    except Exception as e:  # report the failure, keep the old data on the site
        log.error("refresh failed: %s", traceback.format_exc())
        site.report(False, round(time.time() - t0), trigger, f"{type(e).__name__}: {e}"[:300])
        return 1


if __name__ == "__main__":
    sys.exit(main())
