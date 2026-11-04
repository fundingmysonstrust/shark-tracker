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

from . import combos as combos_mod
from . import feed as feed_mod
from . import kalshi
from . import polymarket as pm
from .beliefs import Position, tally, to_beliefs
from .netclient import FetchError, PoliteClient
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


# Positions are saved between refreshes as short lists (to keep the saved
# state small); market titles are saved once per market in state["titles"].
POS_FIELDS = ("conditionId", "outcome", "outcomeIndex", "oppositeOutcome", "size", "avgPrice",
              "initialValue", "currentValue", "curPrice", "eventSlug", "endDate", "timestamp")


def _r(x: Any, nd: int = 2) -> Any:
    return round(x, nd) if isinstance(x, float) else x


def _compact_position(p: dict) -> list:
    out = [p.get(k) for k in POS_FIELDS]
    out[4], out[6], out[7] = _r(out[4]), _r(out[6]), _r(out[7])
    out[5], out[8] = _r(out[5], 4), _r(out[8], 4)
    out[10] = (out[10] or "")[:10]
    return out


def _expand_position(row: Any) -> dict:
    if isinstance(row, dict):  # older saved format
        return row
    return dict(zip(POS_FIELDS, row))


def _slim_rows(rows: list[dict], min_money_single: float = 500, cap: int = 3000) -> list[dict]:
    """Keep the website download small: drop one-shark rows with little money behind them."""
    keep = [r for r in rows if r["total"]["A"] + r["total"]["B"] >= 2 or r["hedged_count"]
            or max(r["money"]["A"], r["money"]["B"]) >= min_money_single]
    for r in keep:
        blk = r["polymarket"]
        for side in ("A", "B"):
            for sh in blk[side]["sharks"]:
                sh.pop("shares", None)
                sh.pop("value", None)
            blk[side].pop("shares", None)
        r.pop("kalshi_title", None)
        r.pop("match", None)
        r["tags"] = (r.get("tags") or [])[:5]
    return keep[:cap]


def _shark_positions(rows: list, titles: dict, markets: dict, cut: float, limit: int = 25) -> list[dict]:
    """A shark's open positions for the detail view: biggest by current value, newest activity first."""
    out = []
    for row in map(_expand_position, rows):
        px = float(row.get("curPrice") or 0)
        if px < cut or px > 1 - cut:
            continue
        cid = row["conditionId"]
        m = markets.get(cid)
        out.append({
            "id": cid, "title": titles.get(cid) or row.get("title") or cid,
            "category": m.category if m else "other", "outcome": row.get("outcome"),
            "cost": _r(row.get("initialValue")), "value": _r(row.get("currentValue")),
            "entry": row.get("avgPrice"), "now": row.get("curPrice"),
            "end": (row.get("endDate") or "")[:10] or None, "at": row.get("timestamp"),
        })
    out.sort(key=lambda r: r["value"] or 0, reverse=True)
    return out[:limit]


def run(user_settings: Optional[dict], decisions: Optional[dict], state: Optional[dict],
        client: Optional[PoliteClient] = None, history_seconds: float = 120,
        positions_seconds: float = 240) -> tuple[dict, dict]:
    t_start = time.time()
    S = load_settings(user_settings)
    state = state or {}
    hist_cache: dict = state.get("history", {})
    pos_cache: dict = state.get("positions", {})
    ev_cache: dict = state.get("events", {})
    titles: dict = state.get("titles", {})
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
             (not t.history or time.time() - t.history.get("fetched_at", 0) > 3 * DAY)]
    # Never-checked traders first (best preliminary score first), then the stalest.
    queue.sort(key=lambda t: (t.history is not None, (t.history or {}).get("fetched_at", 0)))
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
    # Oldest (or never) fetched first, so every shark gets its turn.
    for w in sorted(sharks, key=lambda w: pos_cache.get(w, {}).get("fetched_at", 0)):
        if time.time() > deadline:
            break
        try:
            rows = pm.fetch_open_positions(client, w, min_value=max(1.0, S["min_position_usd"]))
            pos_cache[w] = {"fetched_at": int(time.time()),
                            "rows": [_compact_position(p) for p in rows]}
            for p in rows:
                titles[p["conditionId"]] = p.get("title")
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
        for p in map(_expand_position, entry["rows"]):
            px = float(p.get("curPrice") or 0)
            if px < cut or px > 1 - cut:
                continue
            cid = p["conditionId"]
            raw_by_market.setdefault(cid, p)
            positions.append(Position(
                platform="polymarket", trader_id=w, market_id=cid, outcome=p["outcome"],
                shares=float(p.get("size") or 0), avg_price=p.get("avgPrice"),
                cost_usd=p.get("initialValue"), value_usd=p.get("currentValue")))

    # Event details: refetch when unknown, older than a day, or missing this market.
    need = [p["eventSlug"] for cid, p in raw_by_market.items()
            if p.get("eventSlug") and (p["eventSlug"] not in ev_cache or
                                       time.time() - ev_cache[p["eventSlug"]].get("_at", 0) > DAY or
                                       cid not in ev_cache[p["eventSlug"]].get("markets", {}))]
    for slug, ev in pm.fetch_events(client, need).items():
        ev["_at"] = int(time.time())
        ev["n_markets"] = len(ev.get("markets") or {})
        ev_cache[slug] = ev
    used_slugs = {p.get("eventSlug") for p in raw_by_market.values()}
    ev_cache = {s: v for s, v in ev_cache.items() if s in used_slugs}
    # Only keep the markets we actually use, so the saved state stays small.
    for ev in ev_cache.values():
        ev["markets"] = {c: m for c, m in (ev.get("markets") or {}).items() if c in raw_by_market}
    titles = {c: t for c, t in titles.items() if c in raw_by_market}

    for cid, p in raw_by_market.items():
        ev = ev_cache.get(p.get("eventSlug")) or {}
        idx = int(p.get("outcomeIndex") or 0)
        mine, other = p["outcome"], p.get("oppositeOutcome") or ("No" if p["outcome"] == "Yes" else "Yes")
        labels = (mine, other) if idx == 0 else (other, mine)
        px = float(p.get("curPrice") or 0)
        price_a = px if idx == 0 else round(1 - px, 4)
        mk = (ev.get("markets") or {}).get(cid, {})
        markets[cid] = Market(
            platform="polymarket", market_id=cid, title=titles.get(cid) or p.get("title") or mk.get("question") or cid,
            category=ev.get("category", "other"),
            url=f"https://polymarket.com/event/{p.get('eventSlug') or ''}",
            spec=Spec(kind="event"), side_labels=labels,
            outcome_side={labels[0]: "A", labels[1]: "B"}, price_a=price_a,
            event_title=ev.get("title") or "", end_date=(p.get("endDate") or "")[:10] or None,
            extra={"event_start": ev.get("start"), "tags": ev.get("tags", []),
                   "group_title": (mk.get("group_title") or None) if ev.get("n_markets", len(ev.get("markets") or {})) > 1 else None,
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
        r["market_id"] = m.market_id
        r["group_title"] = m.extra.get("group_title")
        r["event_start"] = m.extra.get("event_start")
        r["end_date"] = m.end_date
        r["tags"] = m.extra.get("tags")
        r["event_date"] = (m.extra.get("event_start") if m.category == "sports" else None) or m.end_date
        for side in ("A", "B"):
            for s in blk[side]["sharks"]:
                w = s["trader_id"]
                s["name"] = names.get(w, w[:10])
                s.update(basis.get((blk["prop_id"], w), {}))
        for h in blk["hedged"]:
            h["name"] = names.get(h["trader_id"], h["trader_id"][:10])

    # 7. Kalshi prices for the same markets (never fails the refresh).
    kstate = state.get("kalshi", {})
    possible: list[dict] = []
    try:
        sweep = kalshi.sweep_catalog(client, kstate)
        res = kalshi.attach_prices(client, rows, kstate, decisions or {}, S)
        possible = res["possible"]
        st = res["stats"]
        status["kalshi"] = {"ok": True, "message": (
            f"Prices shown for {st['matched']} matched markets; {st['possible']} possible matches to review. "
            f"Kalshi doesn't publish other traders' positions, so shark counts are Polymarket only."
            + ("" if sweep["done"] else f" Still building the list of Kalshi markets ({sweep['events']} so far)."))}
    except Exception as e:  # noqa: BLE001 - Kalshi problems must not stop the refresh
        log.warning("Kalshi step failed: %s", e)
        status["kalshi"] = {"ok": False, "message": f"Kalshi prices unavailable this time ({type(e).__name__})."}

    # 8. Recommended combos with live returns (never fails the refresh).
    combo_data, combo_state = {"combos": [], "record": {}}, state.get("combos", {})
    try:
        combo_data, combo_state = combos_mod.update(client, rows, state.get("combos"))
    except Exception as e:  # noqa: BLE001
        log.warning("Combos step failed: %s", e)

    all_rows = len(rows)
    rows = _slim_rows(rows)

    # 9. Live trade feed and profit curves (never fail the refresh).
    shark_info = {w: {"name": traders[w].name, "score": scores[w].get("overall", {}).get("score",
                      max(v["score"] for v in scores[w].values()))} for w in sharks}
    feed_state, pnl_state = state.get("feed", {}), state.get("pnl", {})
    feed_data = {"trades": [], "since": None}
    try:
        feed_state = feed_mod.update_feed(client, set(sharks), feed_state)
        feed_data = feed_mod.feed_for_site(feed_state, shark_info)
        ranked = sorted(sharks, key=lambda w: -shark_info[w]["score"])
        pnl_state = feed_mod.update_pnl(client, ranked, pnl_state)
    except Exception as e:  # noqa: BLE001
        log.warning("Feed step failed: %s", e)
    pnl_state = {w: v for w, v in pnl_state.items() if w in set(sharks)}

    trader_out = {}
    for w in sharks:
        t = traders[w]
        trader_out[w] = {"name": t.name, "profile_url": t.profile_url, "scores": scores[w],
                         "history": t.history, "boards": {f"{c}|{p}": v for (c, p), v in t.boards.items()},
                         "positions_fetched_at": pos_cache.get(w, {}).get("fetched_at"),
                         "pnl": pnl_state.get(w),
                         "positions": _shark_positions(pos_cache.get(w, {}).get("rows", []), titles, markets, cut)}

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
    # Totals for the overview slide: everyone on the overall leaderboards, and the sharks.
    def _totals(ws):
        out = {}
        for window in ("all", "30d"):
            rows_ = [traders[w].boards[("overall", window)] for w in ws if ("overall", window) in traders[w].boards]
            out[window] = {"traders": len(rows_), "pnl": round(sum(r["pnl"] for r in rows_)),
                           "volume": round(sum(r["vol"] for r in rows_))}
        return out
    totals = {"leaderboard": _totals(list(traders)), "sharks": _totals(sharks),
              "leaderboard_traders": len(traders), "sharks_count": len(sharks)}

    data = {"generated_at": int(time.time()), "settings": S, "status": status, "totals": totals,
            "rows": rows, "rows_total": all_rows, "traders": trader_out, "skipped": skipped,
            "possible_matches": possible, "combos": combo_data, "feed": feed_data}
    new_state = {"history": hist_cache, "positions": pos_cache, "events": ev_cache, "titles": titles,
                 "kalshi": kstate, "combos": combo_state, "feed": feed_state, "pnl": pnl_state}
    log.info("refresh done in %ss: %d rows, %s", took, len(rows), status["polymarket"]["message"])
    return data, new_state
