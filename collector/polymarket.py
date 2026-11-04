"""
Polymarket data (public, no login, no key).

Endpoints used (Data API v2, checked live 2026-11-04; v1 was retired 2026-11-02):
  /v2/leaderboard  ?category=&time_period=MONTH|ALL&sort_by=PNL&limit<=1000&cursor=
  /v2/positions    ?user=<wallet>&status=OPEN|CLOSED|REDEEMABLE|REDEEMABLE_LOST
                   &sort_by=&sort_direction=&limit=&cursor=
  /v2/user-stats   ?user=<wallet>   -> data.trades = number of markets ever traded
  gamma-api /events ?slug=<eventSlug> -> titles, tags, teams, start times

v2 rows are converted to the field names the rest of the code uses
(conditionId, curPrice, ...), see `_v1_shape`.

How stats are built for one trader:
  - 30-day and all-time profit + money traded come straight from the
    leaderboards (per category, so a sports record drives sports scores).
  - Win rate is computed from their most recent settled positions
    (closed positions + resolved positions still waiting to be claimed).
    Polymarket doesn't split this by category, so the same win rate is
    used for every category; the site says so.
  - Number of settled markets = markets traded minus markets still open.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .netclient import FetchError, PoliteClient
from .scoring import TraderStats

log = logging.getLogger("shark.polymarket")

DATA = "https://data-api.polymarket.com/v2"
GAMMA = "https://gamma-api.polymarket.com"

# Polymarket's leaderboard categories (OVERALL = every category together).
CATEGORIES = ["SPORTS", "POLITICS", "ECONOMICS", "FINANCE", "CRYPTO",
              "CULTURE", "TECH", "WEATHER", "MENTIONS"]
PERIODS = {"MONTH": "30d", "ALL": "all"}

PROFILE_URL = "https://polymarket.com/profile/{wallet}"


@dataclass
class PMTrader:
    wallet: str
    name: str
    # (category, "30d"/"all") -> {"pnl":, "vol":, "rank":}
    boards: dict[tuple[str, str], dict[str, float]] = field(default_factory=dict)
    history: Optional[dict[str, Any]] = None   # filled by fetch_history

    @property
    def profile_url(self) -> str:
        return PROFILE_URL.format(wallet=self.wallet)


def _pages(client: PoliteClient, path: str, params: dict, max_rows: int, ttl: float = 0) -> list[dict]:
    """Follow v2 cursors until `max_rows` rows or the end."""
    out: list[dict] = []
    cursor = None
    while len(out) < max_rows:
        q = dict(params)
        if cursor:
            q["cursor"] = cursor
        d = client.get_json(f"{DATA}{path}", q, ttl=ttl) or {}
        rows = d.get("data") or []
        out.extend(rows)
        cursor = (d.get("pagination") or {}).get("next_cursor")
        if not rows or not cursor:
            break
    return out[:max_rows]


def _v1_shape(r: dict) -> dict:
    """Map a v2 position row onto the field names used elsewhere."""
    return {
        "conditionId": r.get("condition_id"), "outcome": r.get("outcome"),
        "outcomeIndex": r.get("outcome_index"), "oppositeOutcome": r.get("opposite_outcome"),
        "size": r.get("current_size"), "avgPrice": r.get("avg_price"),
        "initialValue": r.get("total_cost_usdc"), "currentValue": r.get("current_value"),
        "curPrice": r.get("current_price"), "title": r.get("title"),
        "eventSlug": r.get("event_slug"), "endDate": r.get("end_date"),
        "redeemable": r.get("redeemable"), "realizedPnl": r.get("realized_pnl"),
        "timestamp": r.get("last_event_at"),
    }


def fetch_leaderboards(client: PoliteClient, depth: int = 1000,
                       categories: Optional[list[str]] = None) -> dict[str, PMTrader]:
    """Top `depth` traders by profit for every category and both time windows."""
    traders: dict[str, PMTrader] = {}
    cats = ["OVERALL"] + (categories or CATEGORIES)
    for cat in cats:
        for period, window in PERIODS.items():
            rows = _pages(client, "/leaderboard", {"category": cat, "time_period": period,
                                                   "sort_by": "PNL", "limit": min(depth, 1000)},
                          max_rows=depth, ttl=600)
            for r in rows:
                w = (r.get("user_id") or r.get("proxy_wallet") or "").lower()
                if not w:
                    continue
                t = traders.setdefault(w, PMTrader(wallet=w, name=r.get("user_name") or w[:10]))
                t.boards[(cat.lower(), window)] = {
                    "pnl": float(r.get("pnl") or 0), "vol": float(r.get("volume") or 0),
                    "rank": int(r.get("rank") or 0)}
    log.info("Polymarket leaderboards: %d unique traders", len(traders))
    return traders


def fetch_history(client: PoliteClient, wallet: str, sample: int = 100,
                  ttl: float = 6 * 3600) -> dict[str, Any]:
    """
    Win rate from the most recent `sample` settled positions, and the total
    number of settled markets. Cached for `ttl` seconds (history changes slowly).
    """
    closed = [_v1_shape(r) for r in _pages(client, "/positions", {
        "user": wallet, "status": "CLOSED", "sort_by": "TIMESTAMP", "sort_direction": "DESC",
        "limit": sample}, max_rows=sample, ttl=ttl)]
    oldest = min((c.get("timestamp") or 0 for c in closed), default=0)

    # Resolved but not yet claimed (winners and losers), from the same time span.
    unclaimed = []
    for status in ("REDEEMABLE", "REDEEMABLE_LOST"):
        rows = _pages(client, "/positions", {"user": wallet, "status": status, "sort_by": "TIMESTAMP",
                                             "sort_direction": "DESC", "limit": 200},
                      max_rows=200, ttl=ttl)
        unclaimed += [_v1_shape(r) for r in rows]
    unclaimed = [u for u in unclaimed if float(u.get("initialValue") or 0) >= 1
                 and (u.get("timestamp") or 0) >= oldest]

    wins = sum(1 for c in closed if (c.get("realizedPnl") or 0) > 0)
    wins += sum(1 for u in unclaimed if (u.get("curPrice") or 0) >= 0.99)
    n = len(closed) + len(unclaimed)

    st = (client.get_json(f"{DATA}/user-stats", {"user": wallet}, ttl=ttl) or {}).get("data") or {}
    return {"wins_sample": wins, "settled_sample": n, "traded": st.get("trades"),
            "biggest_win": st.get("biggest_win"), "joined": st.get("join_date"), "fetched_at": int(time.time())}


def fetch_open_positions(client: PoliteClient, wallet: str, min_value: float = 1.0,
                         max_pages: int = 3, ttl: float = 0) -> list[dict]:
    """
    Open (not yet resolved) positions for one wallet, biggest first.
    Stops paging once positions drop below `min_value` dollars.
    """
    out: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        q = {"user": wallet, "status": "OPEN", "sort_by": "CURRENT_VALUE",
             "sort_direction": "DESC", "limit": 500}
        if cursor:
            q["cursor"] = cursor
        d = client.get_json(f"{DATA}/positions", q, ttl=ttl) or {}
        rows = [_v1_shape(r) for r in d.get("data") or []]
        # OPEN also includes decided-but-unclaimed winners: skip those.
        out.extend(r for r in rows if not r["redeemable"] and float(r.get("currentValue") or 0) >= min_value)
        cursor = (d.get("pagination") or {}).get("next_cursor")
        if not cursor or not rows or float(rows[-1].get("currentValue") or 0) < min_value:
            break
    return out


def stats_for(t: PMTrader, category: str, open_count: Optional[int] = None) -> Optional[TraderStats]:
    """TraderStats for one category (or 'overall'). None if not on that board."""
    recent = t.boards.get((category, "30d"))
    alltime = t.boards.get((category, "all"))
    if recent is None and alltime is None:
        return None
    s = TraderStats(
        pnl_30d=recent["pnl"] if recent else None,
        pnl_all=alltime["pnl"] if alltime else None,
        volume_30d=recent["vol"] if recent else None,
        volume_all=alltime["vol"] if alltime else None,
    )
    s.sources["profit"] = f"Polymarket {category} leaderboard"
    if recent is None:
        s.sources["note_30d"] = "Not in the top of the 30-day board, so 30-day profit unknown."
    if alltime is None:
        s.sources["note_all"] = "Not in the top of the all-time board, so all-time profit unknown."
    h = t.history
    if h and h.get("settled_sample"):
        rate = h["wins_sample"] / h["settled_sample"]
        settled = h["settled_sample"]
        if h.get("traded"):
            settled = max(settled, int(h["traded"]) - (open_count or 0))
        s.settled = settled
        s.wins = round(rate * settled)
        s.sources["win_rate"] = (f"Last {h['settled_sample']} settled positions (all categories): "
                                 f"{h['wins_sample']} profitable")
    return s


# Gamma tag slugs -> our category names. First match wins.
TAG_CATEGORY = [
    ("sports", {"sports", "nfl", "nba", "mlb", "nhl", "soccer", "ncaa", "cfb", "cbb", "tennis",
                "golf", "ufc", "mma", "boxing", "f1", "esports", "cricket", "games"}),
    ("economics", {"economy", "economics", "fed", "fed-rates", "inflation", "cpi", "jobs",
                   "gdp", "recession", "interest-rates", "unemployment", "macro"}),
    ("finance", {"finance", "stocks", "business", "ipo", "ipos", "commodities", "equities",
                 "earnings", "companies", "oil"}),
    ("crypto", {"crypto", "bitcoin", "ethereum", "solana", "crypto-prices"}),
    ("politics", {"politics", "elections", "us-election", "geopolitics", "world", "trump",
                  "congress", "senate", "global-elections", "world-elections"}),
    ("tech", {"tech", "ai", "science", "technology"}),
    ("weather", {"weather", "climate", "temperature", "hurricanes"}),
    ("mentions", {"mentions", "mention-markets"}),
    ("culture", {"culture", "pop-culture", "entertainment", "movies", "music", "celebrities",
                 "awards", "tv"}),
]


def category_from_tags(tags: list[dict]) -> str:
    slugs = {(t.get("slug") or "").lower() for t in tags or []}
    for cat, keys in TAG_CATEGORY:
        if slugs & keys:
            return cat
    return "other"


def fetch_events(client: PoliteClient, slugs: list[str], batch: int = 20) -> dict[str, dict]:
    """
    Event details for many slugs, 20 per request. Returns slug -> compact
    record: title, category, start time, tags, and each market's question.
    """
    out: dict[str, dict] = {}
    slugs = sorted(set(s for s in slugs if s))
    for i in range(0, len(slugs), batch):
        chunk = slugs[i:i + batch]
        try:
            rows = client.get_json(f"{GAMMA}/events", {"slug": chunk})
        except FetchError as e:
            log.warning("Gamma batch failed: %s", e)
            continue
        for e in rows or []:
            out[e["slug"]] = {
                "title": e.get("title"),
                "category": category_from_tags(e.get("tags")),
                "tags": [t.get("label") for t in e.get("tags") or []][:8],
                "start": e.get("startTime") or e.get("eventDate"),
                "end": e.get("endDate"),
                "series": e.get("seriesSlug"),
                "markets": {m.get("conditionId"): {
                    "question": m.get("question"),
                    "group_title": m.get("groupItemTitle"),
                    "sports_type": m.get("sportsMarketType"),
                    "line": m.get("line"),
                    "game_start": m.get("gameStartTime"),
                    "slug": m.get("slug"),
                } for m in e.get("markets") or []},
            }
    return out
