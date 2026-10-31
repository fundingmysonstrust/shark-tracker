"""
Kalshi prices for markets that also exist on Polymarket.

Kalshi's official public API (no login) only has market data, not other
traders' positions, so Kalshi is used for its PRICE on matched markets.

Polite by design (~1 request/second, backoff on 429):
  * A catalog of open Kalshi events (titles only) is built up a few pages
    per refresh and kept in `state`; a full sweep takes several refreshes
    and is repeated every few hours.
  * For each Polymarket row, the most similar Kalshi events are looked up
    (one request per event, cached ~10 minutes) and their individual
    markets compared with the hard rules in matcher.py.
  * Matches at or above the auto threshold show Kalshi's price; lower
    ones go to "Possible matches" for the user to confirm on the website.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

from .aliases import norm
from .matcher import Spec, compare_specs, event_tokens, jaccard, parse_threshold
from .netclient import FetchError, PoliteClient

log = logging.getLogger("shark.kalshi")

API = "https://api.elections.kalshi.com/trade-api/v2"

# Very large families of near-identical events that never correspond to a
# Polymarket market we show (vote shares, turnout, state legislature seats).
NOISE_SERIES = re.compile(r"^(KXVOTEGENERAL|KXMIDTERMMOV|KXMIDTERMVOTETURN|KXSTATELEG|KXMVE)")

SWEEP_PAGES_PER_RUN = 12
SWEEP_EVERY = 3 * 3600
EVENT_TTL = 600


def _price(m: dict) -> Optional[float]:
    """Mid of bid/ask when both exist, else last traded price. 0-1."""
    def f(k):
        try:
            v = m.get(k)
            return None if v in (None, "") else float(v)
        except (TypeError, ValueError):
            return None
    bid, ask, last = f("yes_bid_dollars"), f("yes_ask_dollars"), f("last_price_dollars")
    if bid is not None and ask is not None and ask > 0 and ask >= bid:
        return round((bid + ask) / 2, 4)
    return last


def sweep_catalog(client: PoliteClient, kstate: dict, pages: int = SWEEP_PAGES_PER_RUN) -> dict:
    """Advance the catalog sweep by up to `pages` pages. Returns progress info."""
    cat = kstate.setdefault("catalog", {})
    sweep = kstate.setdefault("sweep", {"cursor": None, "started": None, "finished": None, "seen": []})
    if sweep["cursor"] is None:
        if sweep.get("finished") and time.time() - sweep["finished"] < SWEEP_EVERY:
            return {"done": True, "events": len(cat)}
        sweep.update({"started": int(time.time()), "seen": []})
    seen = set(sweep.get("seen") or [])
    for _ in range(pages):
        params = {"status": "open", "limit": 200}
        if sweep["cursor"]:
            params["cursor"] = sweep["cursor"]
        d = client.get_json(f"{API}/events", params)
        for e in d.get("events") or []:
            if NOISE_SERIES.match(e.get("series_ticker") or ""):
                continue
            cat[e["event_ticker"]] = [e.get("title") or "", e.get("sub_title") or "",
                                      e.get("category") or "", e.get("series_ticker") or ""]
            seen.add(e["event_ticker"])
        sweep["cursor"] = d.get("cursor") or None
        if not sweep["cursor"]:
            # Finished a full pass: drop events that are no longer open.
            for t in [t for t in cat if t not in seen]:
                del cat[t]
            sweep.update({"finished": int(time.time()), "seen": []})
            return {"done": True, "events": len(cat)}
    sweep["seen"] = sorted(seen)
    return {"done": False, "events": len(cat)}


def fetch_event_markets(client: PoliteClient, ticker: str, kstate: dict) -> Optional[dict]:
    cache = kstate.setdefault("event_cache", {})
    hit = cache.get(ticker)
    if hit and time.time() - hit["_at"] < EVENT_TTL:
        return hit
    try:
        d = client.get_json(f"{API}/events/{ticker}", {"with_nested_markets": "true"})
    except FetchError as e:
        log.warning("Kalshi event %s: %s", ticker, e)
        return hit
    ev = d.get("event") or {}
    markets = ev.get("markets") or d.get("markets") or []
    rec = {"_at": int(time.time()), "title": ev.get("title") or "", "sub_title": ev.get("sub_title") or "",
           "series": ev.get("series_ticker") or "",
           "markets": [{"ticker": m["ticker"], "title": m.get("title") or "",
                        "yes": m.get("yes_sub_title") or "", "close": (m.get("close_time") or "")[:10],
                        "price": _price(m), "status": m.get("status")}
                       for m in markets if m.get("status") in (None, "active", "open", "initialized")]}
    cache[ticker] = rec
    return rec


def _pm_spec(row: dict) -> Spec:
    q = row["title"]
    comp, thr = parse_threshold(q)
    group = row.get("group_title")
    return Spec(kind="event", tokens=event_tokens((row.get("event_title") or "") + " " + q),
                deadline=(row.get("end_date") or "")[:10] or None, threshold=thr, comparator=comp,
                outcome=norm(group) if group else None)


_NOT_A_NAME = re.compile(r"\d|^(before|by|after|above|below|at least|at most|over|under|more|less|yes|no)\b", re.I)


def _named_outcome(yes_sub_title: str) -> Optional[str]:
    """A candidate/team name like 'Pete Ricketts' - not a date or number like 'Before Dec 31'."""
    if not yes_sub_title or _NOT_A_NAME.search(yes_sub_title.strip()):
        return None
    return norm(yes_sub_title)


def _k_spec(ev: dict, m: dict, multi: bool) -> Spec:
    text = m["title"] + " " + m["yes"]
    comp, thr = parse_threshold(text)
    return Spec(kind="event", tokens=event_tokens(ev["title"] + " " + m["title"]),
                deadline=m["close"] or None, threshold=thr, comparator=comp,
                outcome=_named_outcome(m["yes"]) if multi else None)


def attach_prices(client: PoliteClient, rows: list[dict], kstate: dict, decisions: dict,
                  settings: dict, max_rows: int = 150, max_event_fetches: int = 40,
                  seconds: float = 60) -> dict[str, Any]:
    """
    Add `row["kalshi"]` (price on the matched Kalshi market) to rows that
    match, and return {"possible": [...], "stats": {...}}.
    Only event-style (non-game) markets are matched for now.
    """
    cat = kstate.get("catalog") or {}
    auto_cut = settings["auto_match_confidence"]
    possible_cut = settings["possible_match_confidence"]
    cat_tokens = {t: event_tokens(v[0] + " " + v[1]) for t, v in cat.items()}
    deadline = time.time() + seconds
    fetches = matched = 0
    possible: list[dict] = []

    for row in rows[:max_rows]:
        if row["category"] == "sports" or time.time() > deadline:
            continue
        pm_spec = _pm_spec(row)
        if not pm_spec.tokens:
            continue
        # Rank Kalshi events by wording similarity to the Polymarket event + question.
        cands = sorted(((jaccard(pm_spec.tokens, tk), t) for t, tk in cat_tokens.items()), reverse=True)[:3]
        best: Optional[dict] = None
        for sim, t in cands:
            if sim < 0.25:
                break
            if t not in kstate.get("event_cache", {}) or \
                    time.time() - kstate["event_cache"][t]["_at"] >= EVENT_TTL:
                if fetches >= max_event_fetches:
                    continue
                fetches += 1
            ev = fetch_event_markets(client, t, kstate)
            if not ev:
                continue
            multi = len(ev["markets"]) > 1
            for m in ev["markets"]:
                pair = f"polymarket|{row['market_id']}||kalshi|{m['ticker']}"
                decided = decisions.get(pair)
                if decided == "not_same":
                    continue
                kspec = _k_spec(ev, m, multi)
                conf, reasons, _ = compare_specs(pm_spec, kspec)
                if (pm_spec.outcome is None) != (kspec.outcome is None) and conf > 0:
                    # e.g. "Will Republicans win?" vs "Winner: Pete Ricketts" - never automatic.
                    conf = min(conf, auto_cut - 0.01)
                    reasons = reasons + ["only one side names a specific outcome"]
                if decided == "same":
                    conf, reasons = 1.0, ["you marked these as the same market"]
                if conf >= possible_cut and (best is None or conf > best["confidence"]):
                    best = {"pair": pair, "confidence": conf, "reasons": reasons, "ticker": m["ticker"],
                            "title": (m["title"] + (f" — {m['yes']}" if multi and m["yes"] else "")),
                            "price": m["price"], "series": ev["series"]}
        if not best:
            continue
        url = f"https://kalshi.com/markets/{best['series'].lower()}" if best["series"] else "https://kalshi.com"
        if best["confidence"] >= auto_cut:
            # Side A on Polymarket is "Yes" for Yes/No questions; Kalshi YES lines up with it.
            yes_is_a = (row["side_labels"][0].lower() == "yes") or bool(row.get("group_title"))
            pa = best["price"] if yes_is_a else None
            row["kalshi"] = {"title": best["title"], "url": url, "ticker": best["ticker"],
                             "price_a": pa, "price_b": None if pa is None else round(1 - pa, 4),
                             "confidence": best["confidence"], "reasons": best["reasons"], "pair": best["pair"]}
            matched += 1
        else:
            possible.append({"pair": best["pair"], "pm_title": row["title"], "pm_url": row["polymarket"]["url"],
                             "kalshi_title": best["title"], "kalshi_url": url, "kalshi_price": best["price"],
                             "pm_price": row["polymarket"]["price_a"], "confidence": best["confidence"],
                             "reasons": best["reasons"], "row_id": row["id"]})

    # Keep the event cache small: only what was used recently.
    ec = kstate.get("event_cache", {})
    kstate["event_cache"] = {t: v for t, v in ec.items() if time.time() - v["_at"] < 6 * 3600}
    return {"possible": possible, "stats": {"matched": matched, "possible": len(possible),
                                            "event_fetches": fetches, "catalog_events": len(cat)}}
