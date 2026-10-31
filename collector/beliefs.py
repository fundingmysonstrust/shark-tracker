"""
Turn raw positions into "what the shark believes", then count sharks per
side of every market.

Input: positions from either platform, already tied to a `Market`:

    Position(platform, trader_id, market_id, outcome, shares, avg_price,
             cost_usd, value_usd)

Steps:
  1. Map the held outcome to side "A" or "B" of the proposition
     (Polymarket "USC" -> side USC; Kalshi "NO" on "Oregon wins?" -> side USC).
  2. Drop positions smaller than `min_position_usd`.
  3. If a trader holds BOTH sides of the same proposition they are
     "hedged": listed, but counted for neither side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .matcher import Market


@dataclass
class Position:
    platform: str
    trader_id: str                 # wallet address / Kalshi username
    market_id: str
    outcome: str                   # "Yes" / "No" / "USC" ...
    shares: float
    avg_price: Optional[float]     # average entry price, 0-1
    cost_usd: Optional[float]      # money put in (shares x avg price)
    value_usd: Optional[float]     # current value


@dataclass
class Belief:
    platform: str
    trader_id: str
    prop_id: str
    side: str                      # "A" | "B"
    shares: float
    avg_price: Optional[float]
    cost_usd: float
    value_usd: Optional[float]
    market_id: str
    outcome: str


def position_size(p: Position) -> float:
    """Dollar size used for the minimum-size filter and the money columns."""
    if p.cost_usd is not None and p.cost_usd > 0:
        return p.cost_usd
    if p.avg_price is not None and p.shares:
        return p.shares * p.avg_price
    return p.value_usd or 0.0


def to_beliefs(positions: list[Position], markets: dict[str, Market],
               market_to_prop: dict[str, str], settings: dict[str, Any]
               ) -> tuple[list[Belief], dict[str, int]]:
    """
    Convert positions into beliefs. Returns (beliefs, counters) where the
    counters explain what was skipped, for the log and status box.
    """
    min_usd = settings["min_position_usd"]
    out: list[Belief] = []
    skipped = {"unknown_market": 0, "too_small": 0, "unknown_outcome": 0}
    for p in positions:
        m = markets.get(p.market_id)
        if m is None:
            skipped["unknown_market"] += 1
            continue
        side = m.outcome_side.get(p.outcome) or m.outcome_side.get(p.outcome.lower()) \
            or m.outcome_side.get(p.outcome.capitalize())
        if side not in ("A", "B"):
            skipped["unknown_outcome"] += 1
            continue
        size = position_size(p)
        if size < min_usd:
            skipped["too_small"] += 1
            continue
        out.append(Belief(platform=p.platform, trader_id=p.trader_id,
                          prop_id=market_to_prop[p.market_id], side=side, shares=p.shares,
                          avg_price=p.avg_price, cost_usd=size, value_usd=p.value_usd,
                          market_id=p.market_id, outcome=p.outcome))
    return out, skipped


def tally(beliefs: list[Belief], is_shark: dict[tuple[str, str], bool]
          ) -> dict[str, dict[str, Any]]:
    """
    Per proposition, merge each trader's beliefs and count sharks per side.

    `is_shark[(platform, trader_id)]` says whether the trader is a shark for
    that proposition's category (scores are category-specific, so the
    caller resolves this per prop; key is (prop_id, trader_id) here).

    Returns prop_id -> {
        "A": {"sharks": [...], "count": n, "cost": $, "shares": s, "avg_price": p},
        "B": {...},
        "hedged": [trader_id, ...],
    }
    """
    # Combine multiple positions by the same trader on the same prop
    # (e.g. Kalshi YES on "USC wins" and NO on "Oregon wins" = both side A).
    per_trader: dict[tuple[str, str], dict[str, list[Belief]]] = {}
    for b in beliefs:
        per_trader.setdefault((b.prop_id, b.trader_id), {"A": [], "B": []})[b.side].append(b)

    result: dict[str, dict[str, Any]] = {}
    for (prop_id, trader_id), sides in per_trader.items():
        if not is_shark.get((prop_id, trader_id)):
            continue
        r = result.setdefault(prop_id, {
            "A": {"sharks": [], "count": 0, "cost": 0.0, "shares": 0.0, "_px_shares": 0.0},
            "B": {"sharks": [], "count": 0, "cost": 0.0, "shares": 0.0, "_px_shares": 0.0},
            "hedged": [],
        })
        if sides["A"] and sides["B"]:
            r["hedged"].append({"trader_id": trader_id,
                                "cost_a": sum(b.cost_usd for b in sides["A"]),
                                "cost_b": sum(b.cost_usd for b in sides["B"])})
            continue
        side = "A" if sides["A"] else "B"
        bs = sides[side]
        cost = sum(b.cost_usd for b in bs)
        shares = sum(b.shares for b in bs)
        px_known = [b for b in bs if b.avg_price is not None]
        avg_price = (sum(b.avg_price * b.shares for b in px_known) / sum(b.shares for b in px_known)
                     if px_known and sum(b.shares for b in px_known) else None)
        s = r[side]
        s["sharks"].append({"trader_id": trader_id, "cost": round(cost, 2), "shares": round(shares, 2),
                            "avg_price": None if avg_price is None else round(avg_price, 4),
                            "value": round(sum(b.value_usd or 0 for b in bs), 2),
                            "outcomes": sorted({b.outcome for b in bs})})
        s["count"] += 1
        s["cost"] += cost
        if avg_price is not None:
            s["shares"] += shares
            s["_px_shares"] += avg_price * shares

    for r in result.values():
        for side in ("A", "B"):
            s = r[side]
            s["avg_price"] = round(s["_px_shares"] / s["shares"], 4) if s["shares"] else None
            s["cost"] = round(s["cost"], 2)
            del s["_px_shares"]
            s["sharks"].sort(key=lambda x: x["cost"], reverse=True)
    return result
