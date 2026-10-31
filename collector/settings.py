"""
Default settings for Shark Tracker.

Every value here can be changed from the website's Settings page. The
website saves the user's choices, and the collector merges them on top of
these defaults at the start of each refresh (see `load_settings`).

Nothing in this file needs to be edited by hand.
"""

from __future__ import annotations

import copy
from typing import Any

DEFAULT_SETTINGS: dict[str, Any] = {
    # A trader is a "shark" when their reliability score is at least this.
    "shark_threshold": 60,

    # How much each signal counts toward the 0-100 reliability score.
    # They don't have to add up to 100; they're turned into proportions.
    "weights": {
        "recent_profit": 20,    # 30-day profit (recent form)
        "alltime_profit": 20,   # all-time profit (track record)
        "win_rate": 15,         # share of settled markets that made money
        "sample_size": 10,      # how many settled markets they have
        "roi": 20,              # profit divided by money traded
        "consistency": 15,      # profitable both recently AND all-time
    },

    # Traders with fewer settled markets than this can't be sharks
    # (protects against one lucky bet).
    "min_settled_markets": 20,

    # Positions smaller than this many dollars are ignored.
    "min_position_usd": 50,

    # Profit amounts that earn full marks. Profit is scored on a log scale
    # between `profit_floor_usd` (0 marks) and the cap (full marks).
    "profit_floor_usd": 1_000,
    "recent_profit_full_marks_usd": 100_000,
    "alltime_profit_full_marks_usd": 1_000_000,

    # ROI that earns full marks (0.10 = 10% profit on money traded).
    "roi_full_marks": 0.10,

    # Win rates: at or below `win_rate_zero` = 0 marks,
    # at or above `win_rate_full` = full marks.
    "win_rate_zero": 0.50,
    "win_rate_full": 0.75,

    # If a trader has no record in a market's category, use their overall
    # score multiplied by this (1.0 = no penalty, 0 = don't count them).
    "off_category_penalty": 0.8,

    # Market matching between Kalshi and Polymarket.
    # Matches at or above `auto_match_confidence` merge automatically.
    # Matches between the two numbers go to "Possible matches" for review.
    "auto_match_confidence": 0.85,
    "possible_match_confidence": 0.55,

    # Flag a row as "split" when the smaller side has at least this share
    # of the sharks (0.4 = e.g. 6 vs 4 or closer).
    "split_flag_ratio": 0.4,

    # How many traders to pull per category per time window (min 100).
    "leaderboard_depth": 100,
}

# Hard limits so a slider can never break the collector.
LIMITS: dict[str, tuple[float, float]] = {
    "shark_threshold": (0, 100),
    "min_settled_markets": (0, 500),
    "min_position_usd": (0, 100_000),
    "profit_floor_usd": (0, 1_000_000),
    "recent_profit_full_marks_usd": (1_000, 100_000_000),
    "alltime_profit_full_marks_usd": (1_000, 1_000_000_000),
    "roi_full_marks": (0.005, 2.0),
    "win_rate_zero": (0.0, 0.95),
    "win_rate_full": (0.05, 1.0),
    "off_category_penalty": (0.0, 1.0),
    "auto_match_confidence": (0.5, 1.0),
    "possible_match_confidence": (0.1, 0.99),
    "split_flag_ratio": (0.1, 0.5),
    "leaderboard_depth": (100, 500),
}


def _clamp(value: Any, lo: float, hi: float, fallback: Any) -> Any:
    """Return value forced into [lo, hi]; fall back if it isn't a number."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return fallback
    v = max(lo, min(hi, v))
    return int(v) if isinstance(fallback, int) else v


def load_settings(user_settings: dict[str, Any] | None) -> dict[str, Any]:
    """
    Merge the user's saved settings on top of the defaults.

    Unknown keys are ignored, numbers are clamped to safe ranges, and
    anything malformed silently falls back to the default. This way a bad
    save can never stop a refresh.
    """
    merged = copy.deepcopy(DEFAULT_SETTINGS)
    if not isinstance(user_settings, dict):
        return merged

    for key, default in DEFAULT_SETTINGS.items():
        if key not in user_settings:
            continue
        if key == "weights":
            given = user_settings["weights"]
            if isinstance(given, dict):
                for wk, wd in default.items():
                    if wk in given:
                        merged["weights"][wk] = _clamp(given[wk], 0, 100, wd)
            continue
        lo, hi = LIMITS.get(key, (float("-inf"), float("inf")))
        merged[key] = _clamp(user_settings[key], lo, hi, default)

    # Keep the two matching thresholds in a sensible order.
    if merged["possible_match_confidence"] >= merged["auto_match_confidence"]:
        merged["possible_match_confidence"] = round(merged["auto_match_confidence"] - 0.05, 2)
    if merged["win_rate_full"] <= merged["win_rate_zero"]:
        merged["win_rate_full"] = min(1.0, merged["win_rate_zero"] + 0.05)
    return merged
