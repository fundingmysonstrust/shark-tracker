"""
Live trade feed and profit curves.

Trade feed
  Polymarket's public global trade feed (/v2/trades, newest first) is paged
  back from "now" to the last trade already seen. Every refresh therefore
  adds only what is new. What is kept:
    * every trade of $500+ made by a shark, for 30 days
    * every other trade of $10,000+ ("whale" trades), for 7 days
  Each kept trade is stored as a short list:
    [timestamp, wallet, name, side, outcome, usd, price, title, condition_id, event_slug, tx]
  The feed starts when the tracker first runs, so the longer ranges (7D, 30D)
  fill up over time. The site says how far back it goes.

Profit curves
  /v2/user-pnl gives a daily profit series per trader. It is fetched for the
  best-scoring sharks, a few per refresh, at most once per day each, and
  stored as up to 60 points: [timestamp, profit].
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .netclient import FetchError, PoliteClient

log = logging.getLogger("shark.feed")
DATA = "https://data-api.polymarket.com/v2"

MIN_SHARK_USD = 500
MIN_WHALE_USD = 10_000
SHARK_KEEP_DAYS, WHALE_KEEP_DAYS = 30, 7
MAX_PAGES = 6
CAP_SHARK, CAP_WHALE = 4000, 1500
DAY = 86400


def _trade_row(t: dict) -> list:
    usd = float(t.get("size") or 0) * float(t.get("price") or 0)
    return [int(t["timestamp"]), (t.get("proxy_wallet") or "").lower(),
            t.get("name") or t.get("pseudonym") or (t.get("proxy_wallet") or "")[:10],
            t.get("side"), t.get("outcome") or "", round(usd, 2), round(float(t.get("price") or 0), 4),
            (t.get("title") or "")[:160], t.get("condition_id") or "", t.get("event_slug") or "",
            (t.get("transaction_hash") or "")[:12]]


def update_feed(client: PoliteClient, shark_ids: set[str], fstate: Optional[dict]) -> dict:
    """Page the global feed back to the last seen trade and return the new feed state."""
    fstate = fstate or {}
    trades: list[list] = fstate.get("trades", [])
    last_ts = int(fstate.get("last_ts", 0))
    seen = {(r[10], r[1], r[8], r[3], r[0]) for r in trades}
    new: list[list] = []
    cursor, newest = None, last_ts
    for _ in range(MAX_PAGES):
        q = {"limit": 1000, "filter_type": "CASH", "filter_amount": MIN_SHARK_USD}
        if cursor:
            q["cursor"] = cursor
        try:
            d = client.get_json(f"{DATA}/trades", q) or {}
        except FetchError as e:
            log.warning("trade feed: %s", e)
            break
        rows = d.get("data") or []
        if not rows:
            break
        reached = False
        for t in rows:
            ts = int(t["timestamp"])
            newest = max(newest, ts)
            if ts <= last_ts:
                reached = True
                continue
            r = _trade_row(t)
            key = (r[10], r[1], r[8], r[3], r[0])
            if key in seen:
                continue
            seen.add(key)
            new.append(r)
        cursor = (d.get("pagination") or {}).get("next_cursor")
        if reached or not cursor:
            break
    trades = new + trades
    now = int(time.time())
    keep_s, keep_w = [], []
    for r in trades:
        if r[1] in shark_ids:
            if now - r[0] <= SHARK_KEEP_DAYS * DAY:
                keep_s.append(r)
        elif r[5] >= MIN_WHALE_USD and now - r[0] <= WHALE_KEEP_DAYS * DAY:
            keep_w.append(r)
    kept = sorted(keep_s[:CAP_SHARK] + keep_w[:CAP_WHALE], key=lambda r: r[0], reverse=True)
    first = min((r[0] for r in kept), default=now)
    log.info("trade feed: %d new, %d kept (%d shark, %d whale)", len(new), len(kept), len(keep_s), len(keep_w))
    return {"trades": kept, "last_ts": newest,
            "since": min(int(fstate.get("since", now)), first) if fstate.get("since") else first}


def feed_for_site(fstate: dict, sharks: dict[str, dict]) -> dict:
    """Trades as objects for the website, flagged with the shark's overall score."""
    out = []
    for ts, w, name, side, outcome, usd, px, title, cid, ev, tx in fstate.get("trades", []):
        sh = sharks.get(w)
        out.append({"t": ts, "w": w, "n": sh["name"] if sh else name, "side": side, "o": outcome, "usd": usd,
                    "px": px, "q": title, "c": cid, "e": ev, "s": sh["score"] if sh else None})
    return {"trades": out, "since": fstate.get("since"), "updated": fstate.get("last_ts")}


def update_pnl(client: PoliteClient, ranked: list[str], pstate: Optional[dict],
               budget: int = 25, max_age: float = DAY) -> dict:
    """Refresh the profit curve of up to `budget` of the best sharks whose curve is missing or a day old."""
    pstate = dict(pstate or {})
    now = int(time.time())
    todo = [w for w in ranked[:250] if now - pstate.get(w, {}).get("at", 0) > max_age][:budget]
    for w in todo:
        try:
            d = (client.get_json(f"{DATA}/user-pnl", {"user": w, "interval": "max", "fidelity": "1d"}) or {}).get("data") or {}
        except FetchError as e:
            log.warning("pnl %s: %s", w[:8], e)
            continue
        pts = d.get("points") or []
        if not pts:
            continue
        step = max(1, len(pts) // 60)
        series = [[int(p["timestamp"]), round(float(p.get("position_pnl") or 0))] for p in pts[::step]]
        last = pts[-1]
        if series[-1][0] != int(last["timestamp"]):
            series.append([int(last["timestamp"]), round(float(last.get("position_pnl") or 0))])
        pstate[w] = {"at": now, "pts": series, "vol": round(float(last.get("volume_usdc") or 0)),
                     "n": int(last.get("trade_count") or 0)}
    return pstate
