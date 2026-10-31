"""
One full refresh: Polymarket leaderboards -> scores -> shark positions -> table rows.

`run()` is pure apart from network calls: it takes the user's settings, the
match decisions and the saved state from the previous refresh, and returns
(data for the website, new state to save). Nothing here writes files.

Time budget
-----------
A refresh must finish in a few minutes while staying at ~1 request/second.
So the slow parts are spread across refreshes and remembered in `state`:
  * trading history (win rate) per trader: refreshed at most every 24h,
    best-scoring traders first, within `history_seconds` per refresh;
  * open positions: every shark every refresh while time allows; any shark
    not reached this time keeps their positions from the last refresh
    (marked with the time they were fetched);
  * event details (titles, categories, start times): cached 24h.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from . import polymarket as pm
from .beliefs import Position, tally, to_beliefs
from .http import FetchError, PoliteClient
from .matcher import Market, Spec, build_props
from .rows import build_rows
from .scoring import score_for_market, score_trader
from .settings import load_settings

log = logging.getLogger("shark.pipeline")

ALL_CATS = ["overall"] + [c.lower() for c in pm.CATEGORIES]
DAY = 24 * 3600


def _score_all(t: pm.PMTrader, S: dict, open_count: Optional[int]) -> dict[str, dict]:
    out = {}
    for cat in ALL_CATS:
        st = pm.stats_for(t, cat, open_count)
        if st is not None:
            out[cat] = score_trader(st, S)
    return out


def _compact_position(p: dict) -> dict:
    keys = ("conditionId", "outcome", "outcomeIndex", "oppositeOutcome", "size", "avgPrice",
            "initialValue", "currentValue", "curPrice", "title", "slug", "eventSlug", "endDate")
    return {k: p.get(k) for k in keys}


def run(user_settings: Optional[dict], decisions: Optional[dict], state: Optional[dict],
        client: Optional[PoliteClient] = None, history_seconds: float = 75,
        positions_seconds: float = 170) -> tuple[dict, dict]:
    t_start = time.time()
    S = load_settings(user_settings)
    state = state or {}
    hist_cache: dict = state.get("history", {})
    pos_cache: dict = state.get("positions", {})
    ev_cache: dict = state.get("events", {})
    client = client or PoliteClient()
    status: dict[str, Any] = {"polymarket": {"ok": False, "message": ""},
                              "kalshi": {"ok": None, "message": "Kalshi does not publish other "
                                         "traders' positions, so only Polymarket sharks are shown."}}

    # 1. Leaderboards (if this fails the whole refresh fails -> site keeps old data).
    traders = pm.fetch_leaderboards(client, depth=S["leaderboard_depth"])
    for w, t in traders.items():
        t.history = hist_cache.get(w)

    # 2. Preliminary scores; refresh trading history for the most promising traders.
    prelim = sorted(((max((v["score"] for v in _score_all(t, S, None).values()), default=0), t)
                     for t in traders.values()), key=lambda x: -x[0])
    cutoff = S["shark_threshold"] - 10
    queue = [t for sc, t in prelim if sc >= cutoff and
             (not t.history or time.time() - t.history.get("fetched_at", 0) > DAY)]
    queue.sort(key=lambda t: t.history is not None)  # never-fetched first
    deadline = time.time() + history_seconds
    hist_done = 0
    for t in queue:
        if time.time() > deadline:
            break
        try:
            t.history = pm.fetch_history(client, t.wallet)
            hist_cache[t.wallet] = t.history
            hist_done += 1
        except FetchError as e:
            log.warning("history %s: %s", t.name, e)
    pending_history = sum(1 for sc, t in prelim if sc >= cutoff and not t.history)

    # 3. Final scores. Only traders whose history is loaded can be sharks.
    scores: dict[str, dict[str, dict]] = {}
    for t in traders.values():
        if not t.history:
            continue
        open_n = len(pos_cache.get(t.wallet, {}).get("rows", [])) or None
        scores[t.wallet] = _score_all(t, S, open_n)
    sharks = sorted((w for w, sc in scores.items() if any(v["is_shark"] for v in sc.values())),
                    key=lambda w: -max(v["score"] for v in scores[w].values()))

    # 4. Open positions for sharks.
    deadline = time.time() + positions_seconds
    fresh = 0
    for w in sharks:
        if time.time() > deadline:
            break
        try:
            rows = pm.fetch_open_positions(client, w, min_value=max(1.0, S["min_position_usd"]))
            pos_cache[w] = {"fetched_at": int(time.time()),
                            "rows": [_compact_position(p) for p in rows]}
            fresh += 1
        except FetchError as e:
            log.warning("positions %s: %s", w, e)
    # forget positions of traders who are no longer sharks
    pos_cache = {w: v for w, v in pos_cache.items() if w in set(sharks)}

    # 5. Markets those positions are in (skip ones that are basically decided).
    cut = S["decided_price_cutoff"]
    markets: dict[str, Market] = {}
    positions: list[Position] = []
    raw_by_market: dict[str, dict] = {}
    for w, entry in pos_cache.items():
        for p in entry["rows"]:
            px = float(p.get("curPrice") or 0)
            if px < cut or px > 1 - cut:
                continue
            cid = p["conditionId"]
            raw_by_market.setdefault(cid, p)
            positions.append(Position(
                platform="polymarket", trader_id=w, market_id=cid, outcome=p["outcome"],
                shares=float(p.get("size") or 0), avg_price=p.get("avgPrice"),
                cost_usd=p.get("initialValue"), value_usd=p.get("currentValue")))

    need = [p["eventSlug"] for p in raw_by_market.values()
            if p.get("eventSlug") and (p["eventSlug"] not in ev_cache or
                                       time.time() - ev_cache[p["eventSlug"]].get("_at", 0) > DAY)]
    for slug, ev in pm.fetch_events(client, need).items():
        ev["_at"] = int(time.time())
        ev_cache[slug] = ev
    used_slugs = {p.get("eventSlug") for p in raw_by_market.values()}
    ev_cache = {s: v for s, v in ev_cache.items() if s in used_slugs}

    for cid, p in raw_by_market.items():
        ev = ev_cache.get(p.get("eventSlug")) or {}
        idx = int(p.get("outcomeIndex") or 0)
        mine, other = p["outcome"], p.get("oppositeOutcome") or ("No" if p["outcome"] == "Yes" else "Yes")
        labels = (mine, other) if idx == 0 else (other, mine)
        px = float(p.get("curPrice") or 0)
        price_a = px if idx == 0 else round(1 - px, 4)
        mk = (ev.get("markets") or {}).get(cid, {})
        markets[cid] = Market(
            platform="polymarket", market_id=cid, title=p.get("title") or mk.get("question") or cid,
            category=ev.get("category", "other"),
            url=f"https://polymarket.com/event/{p.get('eventSlug') or ''}",
            spec=Spec(kind="event"), side_labels=labels,
            outcome_side={labels[0]: "A", labels[1]: "B"}, price_a=price_a,
            event_title=ev.get("title") or "", end_date=(p.get("endDate") or "")[:10] or None,
            extra={"event_start": ev.get("start"), "tags": ev.get("tags", []),
                   "sports_type": mk.get("sports_type"), "event_slug": p.get("eventSlug")})

    # 6. Beliefs -> tallies -> rows.
    props, m2p = build_props(list(markets.values()))
    beliefs, skipped = to_beliefs(positions, markets, m2p, S)
    is_shark: dict[tuple[str, str], bool] = {}
    basis: dict[tuple[str, str], dict] = {}
    for b in beliefs:
        key = (b.prop_id, b.trader_id)
        if key in is_shark:
            continue
        sc = score_for_market(scores.get(b.trader_id, {}), props[b.prop_id].category, S)
        is_shark[key] = bool(sc and sc["is_shark"])
        if sc:
            basis[key] = {"score": sc["score"], "basis": sc["basis"]}
    tallies = tally(beliefs, is_shark)
    rows = build_rows(props, {}, {"auto": [], "possible": []}, tallies, S)

    names = {w: traders[w].name for w in traders}
    for r in rows:
        blk = r["polymarket"]
        m = markets[props[blk["prop_id"]].market_ids[0]]
        r["event_title"] = m.event_title
        r["event_start"] = m.extra.get("event_start")
        r["end_date"] = m.end_date
        r["tags"] = m.extra.get("tags")
        r["event_date"] = (m.extra.get("event_start") if m.category == "sports" else None) or m.end_date
        for side in ("A", "B"):
            for s in blk[side]["sharks"]:
                w = s["trader_id"]
                s["name"] = names.get(w, w[:10])
                s.update(basis.get((blk["prop_id"], w), {}))
                s["fetched_at"] = pos_cache.get(w, {}).get("fetched_at")
        for h in blk["hedged"]:
            h["name"] = names.get(h["trader_id"], h["trader_id"][:10])

    trader_out = {}
    for w in sharks:
        t = traders[w]
        trader_out[w] = {"name": t.name, "profile_url": t.profile_url, "scores": scores[w],
                         "history": t.history, "boards": {f"{c}|{p}": v for (c, p), v in t.boards.items()},
                         "positions_fetched_at": pos_cache.get(w, {}).get("fetched_at")}

    took = round(time.time() - t_start)
    status["polymarket"] = {
        "ok": True,
        "message": (f"{len(traders)} leaderboard traders, {len(sharks)} sharks, "
                    f"positions refreshed for {fresh} of {len(sharks)} this time."
                    + (f" Still loading trading history for {pending_history} traders "
                       "(a little more each refresh)." if pending_history else "")),
        "traders": len(traders), "sharks": len(sharks), "positions_fresh": fresh,
        "history_loaded_this_run": hist_done, "history_pending": pending_history,
        "requests": client.stats["requests"], "errors": client.stats["errors"],
        "seconds": took}
    data = {"generated_at": int(time.time()), "settings": S, "status": status,
            "rows": rows, "traders": trader_out, "skipped": skipped}
    new_state = {"history": hist_cache, "positions": pos_cache, "events": ev_cache}
    log.info("refresh done in %ss: %d rows, %s", took, len(rows), status["polymarket"]["message"])
    return data, new_state
