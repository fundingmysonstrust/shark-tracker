"""
Entry point for the scheduled refresh (GitHub Actions).

1. Ask the website for the user's settings, match decisions and saved state.
2. Run one refresh.
3. Send the new data + state back. If the refresh fails, send a failure
   note instead, so the website keeps the last good data and says so.

Needs two environment variables:
  SITE_URL    e.g. https://shark-tracker.<name>.workers.dev
  INGEST_KEY  shared secret (GitHub secret + Cloudflare secret)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from collector.pipeline import run  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("shark.http").setLevel(logging.WARNING)
log = logging.getLogger("shark.remote")


def main() -> int:
    site = os.environ["SITE_URL"].rstrip("/")
    headers = {"Authorization": "Bearer " + os.environ["INGEST_KEY"],
               "User-Agent": "shark-tracker-collector"}
    trigger = os.environ.get("TRIGGER", "schedule")
    t0 = time.time()
    r = requests.get(site + "/api/collector/input", headers=headers, timeout=60)
    r.raise_for_status()
    inp = r.json()
    state = inp.get("state") or {}
    try:
        data, state = run(inp.get("settings"), inp.get("decisions"), state)
        body = {"ok": True, "data": data, "state": state, "seconds": round(time.time() - t0),
                "trigger": trigger, "message": data["status"]["polymarket"]["message"]}
    except Exception as e:  # report the failure, keep the old data on the site
        log.error("refresh failed: %s", traceback.format_exc())
        body = {"ok": False, "seconds": round(time.time() - t0), "trigger": trigger,
                "message": f"{type(e).__name__}: {e}"[:300]}
    raw = json.dumps(body).encode()
    log.info("uploading %.1f MB", len(raw) / 1e6)
    r = requests.post(site + "/api/collector/output", data=raw, timeout=120,
                      headers={**headers, "Content-Type": "application/json"})
    r.raise_for_status()
    log.info("saved: %s", r.text)
    return 0 if body["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
