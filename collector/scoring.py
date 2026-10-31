"""
Reliability score (0-100) for a trader in one category.

The score blends six signals, each turned into a 0-1 "mark":

  recent_profit   30-day profit, log scale ($1k -> 0, $100k -> 1 by default)
  alltime_profit  all-time profit, log scale ($1k -> 0, $1M -> 1 by default)
  win_rate        share of settled markets that made money (50% -> 0, 75% -> 1)
  sample_size     number of settled markets (min -> 0.2, 5x min -> 1)
  roi             profit / money traded (0% -> 0, 10% -> 1)
  consistency     1.0 profitable in both windows, 0.5 all-time only,
                  0.25 recent only, 0 neither

Weights come from settings. If a signal is missing (the platform doesn't
publish it and it can't be computed), it is dropped and the remaining
weights are scaled up so the score stays out of 100. The missing signals
are listed in the breakdown so the website can show what was missing.

A trader with fewer settled markets than `min_settled_markets` gets a
score but is never counted as a shark ("not enough history").
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TraderStats:
    """Everything we know about one trader in one category."""

    pnl_30d: Optional[float] = None       # dollars
    pnl_all: Optional[float] = None       # dollars
    volume_30d: Optional[float] = None    # dollars traded
    volume_all: Optional[float] = None    # dollars traded
    wins: Optional[int] = None            # settled markets with profit > 0
    settled: Optional[int] = None         # settled markets in total
    # Optional: notes about where numbers came from (shown on the site).
    sources: dict[str, str] = field(default_factory=dict)


def _log_mark(value: Optional[float], floor: float, cap: float) -> Optional[float]:
    """0 at/below floor, 1 at/above cap, log-scaled in between."""
    if value is None:
        return None
    if value <= floor or value <= 0:
        return 0.0
    if value >= cap:
        return 1.0
    lo = math.log10(max(floor, 1.0))
    hi = math.log10(cap)
    return (math.log10(value) - lo) / (hi - lo)


def _linear_mark(value: Optional[float], zero: float, full: float) -> Optional[float]:
    """0 at/below zero, 1 at/above full, linear in between."""
    if value is None:
        return None
    if full == zero:
        return 1.0 if value >= full else 0.0
    return max(0.0, min(1.0, (value - zero) / (full - zero)))


def roi_of(stats: TraderStats) -> Optional[float]:
    """Profit divided by money traded, preferring the all-time window."""
    if stats.pnl_all is not None and stats.volume_all:
        return stats.pnl_all / stats.volume_all
    if stats.pnl_30d is not None and stats.volume_30d:
        return stats.pnl_30d / stats.volume_30d
    return None


def score_trader(stats: TraderStats, settings: dict[str, Any]) -> dict[str, Any]:
    """
    Score one trader in one category.

    Returns a JSON-friendly breakdown:
      {
        "score": 73.4,
        "eligible": True,            # meets min settled markets
        "is_shark": True,
        "components": {name: {"mark": 0.8, "weight": 20, "points": 16.0,
                              "value": <raw number>, "explain": "..."}},
        "missing": ["win_rate", ...],
        "notes": [...],
      }
    """
    w = settings["weights"]
    floor = settings["profit_floor_usd"]
    min_settled = settings["min_settled_markets"]

    win_rate = None
    if stats.settled:
        win_rate = (stats.wins or 0) / stats.settled
    roi = roi_of(stats)

    # Consistency needs at least one profit window to say anything.
    consistency = None
    if stats.pnl_30d is not None or stats.pnl_all is not None:
        recent_ok = (stats.pnl_30d or 0) > 0
        all_ok = (stats.pnl_all or 0) > 0
        if stats.pnl_30d is None or stats.pnl_all is None:
            # Only one window known: judge on what we have, at half strength.
            consistency = 0.5 if (recent_ok or all_ok) else 0.0
        else:
            consistency = {(True, True): 1.0, (False, True): 0.5,
                           (True, False): 0.25, (False, False): 0.0}[(recent_ok, all_ok)]

    sample = None
    if stats.settled is not None:
        if min_settled <= 0:
            sample = 1.0
        elif stats.settled < min_settled:
            sample = 0.2 * stats.settled / min_settled
        else:
            sample = min(1.0, 0.2 + 0.8 * (stats.settled - min_settled) / (4 * min_settled))

    raw = {
        "recent_profit": (stats.pnl_30d, _log_mark(stats.pnl_30d, floor, settings["recent_profit_full_marks_usd"]),
                          "30-day profit"),
        "alltime_profit": (stats.pnl_all, _log_mark(stats.pnl_all, floor, settings["alltime_profit_full_marks_usd"]),
                           "All-time profit"),
        "win_rate": (win_rate, _linear_mark(win_rate, settings["win_rate_zero"], settings["win_rate_full"]),
                     "Win rate on settled markets"),
        "sample_size": (stats.settled, sample, "Number of settled markets"),
        "roi": (roi, _linear_mark(roi, 0.0, settings["roi_full_marks"]), "Profit ÷ money traded"),
        "consistency": (consistency, consistency, "Profitable recently and all-time"),
    }

    available_weight = sum(w[k] for k, (_, mark, _) in raw.items() if mark is not None)
    components: dict[str, Any] = {}
    missing: list[str] = []
    total = 0.0
    for key, (value, mark, label) in raw.items():
        if mark is None:
            missing.append(key)
            components[key] = {"label": label, "value": None, "mark": None,
                               "weight": w[key], "points": None}
            continue
        # Scale weights up to fill 100 when some signals are missing.
        share = (w[key] / available_weight) if available_weight else 0.0
        points = 100.0 * share * mark
        total += points
        components[key] = {"label": label, "value": value, "mark": round(mark, 3),
                           "weight": w[key], "points": round(points, 1)}

    notes = []
    eligible = True
    if stats.settled is None:
        # Can't verify the minimum; allow it but say so.
        notes.append("Settled-market count unavailable; minimum-history rule not applied.")
    elif stats.settled < min_settled:
        eligible = False
        notes.append(f"Only {stats.settled} settled markets (needs {min_settled}).")
    if available_weight == 0:
        eligible = False
        notes.append("No usable stats.")

    score = round(total, 1)
    return {
        "score": score,
        "eligible": eligible,
        "is_shark": eligible and score >= settings["shark_threshold"],
        "components": components,
        "missing": missing,
        "notes": notes,
        "sources": dict(stats.sources),
    }


def score_for_market(trader_scores: dict[str, dict[str, Any]], category: str,
                     settings: dict[str, Any]) -> Optional[dict[str, Any]]:
    """
    Pick the score that applies to a market in `category`.

    A trader's record in that category is used when it exists ("category
    fit"). Otherwise their overall record is used with a penalty, and the
    result says so.
    """
    if category in trader_scores:
        out = dict(trader_scores[category])
        out["basis"] = category
        return out
    overall = trader_scores.get("overall")
    if overall is None:
        return None
    out = dict(overall)
    penalty = settings["off_category_penalty"]
    out["score"] = round(overall["score"] * penalty, 1)
    out["is_shark"] = overall["eligible"] and out["score"] >= settings["shark_threshold"]
    out["basis"] = "overall"
    out["notes"] = list(overall.get("notes", [])) + [
        f"No {category} record found; used overall record × {penalty:g}."]
    return out
