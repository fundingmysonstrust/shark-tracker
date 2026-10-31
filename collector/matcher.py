"""
Cross-platform market matching.

Every market from either platform is first turned into a `Spec`: a
platform-neutral description of WHAT the market is about.

  Sports spec:  league, the two teams, game date (Eastern), market type
                (moneyline / spread / total / team_win / draw / prop),
                line (for spread/total), which team the spread is on.
  Event spec:   the important words of the question, the deadline date,
                the number threshold and its direction (above/below/...),
                and the specific outcome (e.g. a candidate's name).

Each market also says which of its outcomes means "side A" and which means
"side B" of the spec, so positions on both platforms can be counted on
the same two sides.

`match_props()` then compares Polymarket specs with Kalshi specs and gives
each pair a confidence from 0 to 1. Hard rules (never broken):

  * different market type          -> 0  (spread is never moneyline)
  * different line / threshold     -> 0  ("CPI > 3.0%" is never "CPI > 3.2%")
  * different teams or league      -> 0
  * game dates more than a day apart -> 0
  * different named outcome        -> 0  ("Smith wins" is never "Jones wins")

The user's own decisions ("Same market" / "Not the same" buttons on the
site) always override the computed confidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Optional

from .aliases import norm

SPORTS_TYPES = ("moneyline", "spread", "total", "team_win", "draw", "prop")

# Leagues where a game can end in a draw: "Team X wins" is then NOT the same
# as "Team Y loses", so each team's win is its own yes/no market.
THREE_WAY_LEAGUES = {"epl", "mls", "ucl", "laliga", "seriea", "bundesliga", "ligue1"}

STOPWORDS = set("""
a an the will be is are was were of in on at by to for from with and or as
than this that these those it its who what which when how does do did has
have had 2024 2025 2026 2027 before after during end yes no market price
""".split())


@dataclass(frozen=True)
class Spec:
    """Platform-neutral description of a market."""

    kind: str                                  # "sports" or "event"
    # --- sports ---
    league: Optional[str] = None
    teams: tuple[str, ...] = ()                # two canonical team ids, sorted
    game_date: Optional[str] = None            # YYYY-MM-DD, Eastern time
    market_type: Optional[str] = None          # one of SPORTS_TYPES
    line: Optional[float] = None               # spread points or total points
    line_team: Optional[str] = None            # team the spread/team_win is about
    prop_text: Optional[str] = None            # normalized prop description
    # --- events ---
    tokens: frozenset = frozenset()            # important words of the question
    deadline: Optional[str] = None             # YYYY-MM-DD
    threshold: Optional[tuple[float, ...]] = None
    comparator: Optional[str] = None           # above / below / between / exact
    outcome: Optional[str] = None              # normalized named outcome
    fuzzy: bool = False                        # some part was guessed

    def group_key(self) -> Optional[tuple]:
        """
        Key that identifies the same proposition WITHIN one platform.

        Kalshi lists one market per team for a game ("USC wins?", "Oregon
        wins?"); both describe the same moneyline, so they share a key.
        Event markets are grouped only by their own id (returns None).
        """
        if self.kind != "sports" or not self.teams or not self.game_date:
            return None
        return ("sports", self.league, self.teams, self.game_date, self.market_type,
                self.line, self.line_team, self.prop_text)


@dataclass
class Market:
    """One market on one platform, already normalized."""

    platform: str                  # "polymarket" | "kalshi"
    market_id: str                 # Kalshi ticker / Polymarket condition id
    title: str                     # the platform's own wording
    category: str                  # sports / politics / economics / ...
    url: str
    spec: Spec
    side_labels: tuple[str, str]   # plain words for side A and side B
    outcome_side: dict[str, str]   # platform outcome ("Yes", "USC", ...) -> "A"/"B"
    price_a: Optional[float] = None  # current price of side A, 0-1
    event_title: str = ""          # parent event wording, if any
    end_date: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------------

_NUM = r"(-?\d+(?:,\d{3})*(?:\.\d+)?)"


def parse_threshold(text: str) -> tuple[Optional[str], Optional[tuple[float, ...]]]:
    """
    Pull a number threshold and its direction out of a question.

      "CPI above 3.0%?"            -> ("above", (3.0,))
      "3.2% or higher"             -> ("above", (3.2,))
      "less than 50,000"           -> ("below", (50000.0,))
      "between 2.5% and 2.75%"     -> ("between", (2.5, 2.75))
      "4.25-4.50%"                 -> ("between", (4.25, 4.5))
    """
    t = (text or "").lower().replace("≥", " at least ").replace("≤", " at most ")
    t = t.replace(">", " above ").replace("<", " below ")
    num = lambda s: float(s.replace(",", ""))

    m = re.search(r"between\s+\$?" + _NUM + r"\s*%?\s*(?:and|-|to)\s*\$?" + _NUM, t)
    if m:
        return "between", (num(m.group(1)), num(m.group(2)))
    m = re.search(r"\$?" + _NUM + r"\s*%?\s*(?:-|–|to)\s*\$?" + _NUM + r"\s*%", t)
    if m:
        return "between", (num(m.group(1)), num(m.group(2)))
    m = re.search(r"(above|over|more than|greater than|higher than|exceeds?|at least|or more)\s*\$?" + _NUM, t)
    if m:
        return "above", (num(m.group(2)),)
    m = re.search(r"\$?" + _NUM + r"\s*%?\+?\s*(or more|or higher|or above|\+|and above)", t)
    if m:
        return "above", (num(m.group(1)),)
    m = re.search(r"(below|under|less than|lower than|at most|or less)\s*\$?" + _NUM, t)
    if m:
        return "below", (num(m.group(2)),)
    m = re.search(r"\$?" + _NUM + r"\s*%?\s*(or less|or lower|or below|and below)", t)
    if m:
        return "below", (num(m.group(1)),)
    return None, None


def event_tokens(text: str) -> frozenset:
    """Important words of a question (numbers and filler words removed)."""
    words = norm(text).split()
    return frozenset(w for w in words if w not in STOPWORDS and not re.fullmatch(r"\d+(\.\d+)?", w)
                     and len(w) > 1)


def jaccard(a: Iterable, b: Iterable) -> float:
    a, b = set(a), set(b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _days_apart(a: Optional[str], b: Optional[str]) -> Optional[int]:
    if not a or not b:
        return None
    try:
        return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Pair confidence
# ----------------------------------------------------------------------------

def compare_specs(p: Spec, k: Spec) -> tuple[float, list[str], bool]:
    """
    Confidence (0-1) that two specs describe the same market, a list of
    plain-English reasons, and whether side A/B must be swapped to line up.
    """
    if p.kind != k.kind:
        return 0.0, ["one is a game, the other is not"], False
    if p.kind == "sports":
        return _compare_sports(p, k)
    return _compare_events(p, k)


def _compare_sports(p: Spec, k: Spec) -> tuple[float, list[str], bool]:
    if p.league != k.league:
        return 0.0, [f"different league ({p.league} vs {k.league})"], False
    if set(p.teams) != set(k.teams) or len(p.teams) != 2:
        return 0.0, ["different teams"], False
    if p.market_type != k.market_type:
        return 0.0, [f"different market type ({p.market_type} vs {k.market_type})"], False
    if p.market_type in ("spread", "total"):
        if p.line is None or k.line is None or abs(p.line - k.line) > 1e-6:
            return 0.0, [f"different line ({p.line} vs {k.line})"], False
    if p.market_type in ("spread", "team_win") and p.line_team != k.line_team:
        return 0.0, ["about different teams"], False

    days = _days_apart(p.game_date, k.game_date)
    reasons = [f"same league, teams, {p.market_type}"]
    if days is None:
        return 0.0, ["game date unknown"], False
    if days == 0:
        conf = 1.0
        reasons.append("same date")
    elif days == 1:
        conf = 0.6  # could be a time-zone edge case, or a different game
        reasons.append("dates 1 day apart")
    else:
        return 0.0, [f"different game dates ({p.game_date} vs {k.game_date})"], False

    if p.market_type == "prop":
        sim = jaccard((p.prop_text or "").split(), (k.prop_text or "").split())
        if p.line != k.line:
            return 0.0, ["different prop line"], False
        conf *= sim
        reasons.append(f"prop wording {sim:.0%} similar")
    if p.fuzzy or k.fuzzy:
        conf *= 0.85
        reasons.append("a team name was guessed")
    return round(conf, 3), reasons, False


def _compare_events(p: Spec, k: Spec) -> tuple[float, list[str], bool]:
    if (p.threshold is None) != (k.threshold is None):
        return 0.0, ["only one has a number threshold"], False
    if p.threshold is not None:
        if p.comparator != k.comparator:
            return 0.0, [f"different direction ({p.comparator} vs {k.comparator})"], False
        if len(p.threshold) != len(k.threshold) or any(
                abs(a - b) > 1e-9 for a, b in zip(p.threshold, k.threshold)):
            return 0.0, [f"different number ({p.threshold} vs {k.threshold})"], False
    if p.outcome and k.outcome and p.outcome != k.outcome:
        return 0.0, [f"different outcome ({p.outcome} vs {k.outcome})"], False

    sim = jaccard(p.tokens, k.tokens)
    if p.outcome and k.outcome:
        sim = min(1.0, sim + 0.15)  # a matching named outcome is strong evidence
    reasons = [f"wording {sim:.0%} similar"]

    days = _days_apart(p.deadline, k.deadline)
    if days is None:
        date_factor = 0.8
        reasons.append("deadline unknown on one side")
    elif days == 0:
        date_factor = 1.0
        reasons.append("same deadline")
    elif days <= 3:
        date_factor = 0.9
        reasons.append(f"deadlines {days} days apart")
    elif days <= 31:
        date_factor = 0.6
        reasons.append(f"deadlines {days} days apart")
    else:
        return 0.0, [f"deadlines {days} days apart"], False

    conf = (sim ** 0.5) * date_factor
    return round(conf, 3), reasons, False


# ----------------------------------------------------------------------------
# Matching many markets
# ----------------------------------------------------------------------------

@dataclass
class Prop:
    """
    One proposition on one platform: one or more markets that mean the same
    thing (e.g. Kalshi's two per-team markets for one game).
    """

    platform: str
    prop_id: str
    spec: Spec
    title: str
    category: str
    url: str
    side_labels: tuple[str, str]
    price_a: Optional[float]
    market_ids: list[str]
    end_date: Optional[str] = None


def build_props(markets: list[Market]) -> tuple[dict[str, Prop], dict[str, str]]:
    """
    Group a platform's markets into propositions.

    Returns (props by prop_id, market_id -> prop_id).
    """
    props: dict[str, Prop] = {}
    market_to_prop: dict[str, str] = {}
    for m in markets:
        gk = m.spec.group_key()
        pid = f"{m.platform}|" + ("|".join(map(str, gk)) if gk else m.market_id)
        if pid not in props:
            props[pid] = Prop(platform=m.platform, prop_id=pid, spec=m.spec, title=m.title,
                              category=m.category, url=m.url, side_labels=m.side_labels,
                              price_a=m.price_a, market_ids=[], end_date=m.end_date)
        p = props[pid]
        p.market_ids.append(m.market_id)
        if p.price_a is None and m.price_a is not None:
            p.price_a = m.price_a
        market_to_prop[m.market_id] = pid
    return props, market_to_prop


def _bucket(spec: Spec) -> tuple:
    """Cheap pre-filter so we don't compare every market with every other."""
    if spec.kind == "sports":
        return ("sports", spec.league, spec.teams)
    return ("event",)


def match_props(pm_props: dict[str, Prop], k_props: dict[str, Prop],
                decisions: dict[str, str], settings: dict[str, Any]) -> dict[str, list[dict]]:
    """
    Pair Polymarket props with Kalshi props.

    `decisions` maps "pm_prop_id||kalshi_prop_id" -> "same" | "not_same"
    (from the website buttons).

    Returns {"auto": [...], "possible": [...]} where each item is
    {"pm": id, "kalshi": id, "confidence": x, "reasons": [...], "decided": ...}.
    Each prop appears in at most one auto match (best confidence wins).
    """
    auto_cut = settings["auto_match_confidence"]
    possible_cut = settings["possible_match_confidence"]

    by_bucket: dict[tuple, list[Prop]] = {}
    for kp in k_props.values():
        by_bucket.setdefault(_bucket(kp.spec), []).append(kp)

    candidates = []
    for pp in pm_props.values():
        for kp in by_bucket.get(_bucket(pp.spec), []):
            if pp.spec.kind == "event" and pp.category != kp.category and \
                    {pp.category, kp.category} - {"politics", "economics", "other"}:
                continue  # e.g. never pair a sports question with an econ one
            pair = f"{pp.prop_id}||{kp.prop_id}"
            decided = decisions.get(pair)
            if decided == "not_same":
                continue
            conf, reasons, _ = compare_specs(pp.spec, kp.spec)
            if decided == "same":
                conf, reasons = 1.0, ["you marked these as the same market"]
            if conf >= possible_cut:
                candidates.append({"pm": pp.prop_id, "kalshi": kp.prop_id, "confidence": conf,
                                   "reasons": reasons, "decided": decided})

    # Greedy: highest confidence first, each prop used once.
    candidates.sort(key=lambda c: (c["decided"] == "same", c["confidence"]), reverse=True)
    used_pm, used_k = set(), set()
    auto, possible = [], []
    for c in candidates:
        if c["pm"] in used_pm or c["kalshi"] in used_k:
            continue
        if c["confidence"] >= auto_cut:
            auto.append(c)
            used_pm.add(c["pm"])
            used_k.add(c["kalshi"])
        else:
            possible.append(c)
    # A "possible" pair whose prop later got an auto match is dropped.
    possible = [c for c in possible if c["pm"] not in used_pm and c["kalshi"] not in used_k]
    return {"auto": auto, "possible": possible}
