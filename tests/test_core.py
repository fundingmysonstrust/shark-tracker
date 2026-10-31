"""Tests for the tricky parts: market matching, team aliases, scoring, beliefs."""
import pytest

from collector.aliases import TeamBook
from collector.beliefs import Position, tally, to_beliefs
from collector.matcher import Market, Spec, build_props, compare_specs, match_props, parse_threshold
from collector.scoring import TraderStats, score_for_market, score_trader
from collector.settings import DEFAULT_SETTINGS, load_settings

S = load_settings({})


def game(date="2026-11-07", mtype="moneyline", line=None, line_team=None, teams=("cfb:ore", "cfb:usc")):
    return Spec(kind="sports", league="cfb", teams=tuple(sorted(teams)), game_date=date,
                market_type=mtype, line=line, line_team=line_team)


# ---------------- matching ----------------
def test_same_game_matches():
    conf, _, _ = compare_specs(game(), game())
    assert conf == 1.0


def test_same_teams_different_dates_never_merge():
    conf, reasons, _ = compare_specs(game("2026-11-07"), game("2026-11-28"))
    assert conf == 0.0 and "different game dates" in reasons[0]


def test_one_day_apart_is_only_possible():
    conf, _, _ = compare_specs(game("2026-11-07"), game("2026-11-08"))
    assert 0 < conf < S["auto_match_confidence"]


def test_spread_vs_moneyline_never_merge():
    conf, _, _ = compare_specs(game(mtype="spread", line=-3.5, line_team="cfb:usc"), game())
    assert conf == 0.0


def test_different_spread_lines_never_merge():
    a = game(mtype="spread", line=-3.5, line_team="cfb:usc")
    b = game(mtype="spread", line=-4.5, line_team="cfb:usc")
    assert compare_specs(a, b)[0] == 0.0


def test_different_teams_never_merge():
    assert compare_specs(game(), game(teams=("cfb:usc", "cfb:ucla")))[0] == 0.0


def test_cpi_thresholds_never_merge():
    def cpi(text):
        comp, thr = parse_threshold(text)
        return Spec(kind="event", tokens=frozenset({"cpi", "october", "inflation"}),
                    deadline="2026-11-12", threshold=thr, comparator=comp)
    assert compare_specs(cpi("CPI above 3.0%?"), cpi("CPI above 3.2%?"))[0] == 0.0
    assert compare_specs(cpi("CPI above 3.0%?"), cpi("Will CPI be more than 3.0%"))[0] > 0.9
    assert compare_specs(cpi("CPI above 3.0%?"), cpi("CPI below 3.0%?"))[0] == 0.0


@pytest.mark.parametrize("text,expected", [
    ("CPI above 3.0%?", ("above", (3.0,))),
    ("3.2% or higher", ("above", (3.2,))),
    ("less than 50,000", ("below", (50000.0,))),
    ("between 2.5% and 2.75%", ("between", (2.5, 2.75))),
    ("4.25-4.50%", ("between", (4.25, 4.5))),
])
def test_parse_threshold(text, expected):
    assert parse_threshold(text) == expected


def test_different_named_outcomes_never_merge():
    a = Spec(kind="event", tokens=frozenset({"nyc", "mayor"}), deadline="2026-11-04", outcome="smith")
    b = Spec(kind="event", tokens=frozenset({"nyc", "mayor"}), deadline="2026-11-04", outcome="jones")
    assert compare_specs(a, b)[0] == 0.0


def test_user_not_same_decision_overrides():
    def mk(platform, mid):
        return Market(platform=platform, market_id=mid, title="x", category="sports", url="",
                      spec=game(), side_labels=("USC", "Oregon"), outcome_side={"USC": "A", "Oregon": "B"})
    pp, _ = build_props([mk("polymarket", "p1")])
    kp, _ = build_props([mk("kalshi", "k1")])
    pair = f"{next(iter(pp))}||{next(iter(kp))}"
    assert len(match_props(pp, kp, {}, S)["auto"]) == 1
    assert match_props(pp, kp, {pair: "not_same"}, S) == {"auto": [], "possible": []}


# ---------------- aliases ----------------
@pytest.mark.parametrize("name", ["USC", "Southern California", "Trojans", "USC Trojans"])
def test_usc_aliases(name):
    assert TeamBook().lookup("cfb", name) == "cfb:usc"


def test_oregon_state_is_not_oregon():
    assert TeamBook().lookup("cfb", "Oregon State") != TeamBook().lookup("cfb", "Oregon")


def test_ambiguous_alias_dropped():
    assert TeamBook().lookup("nfl", "New York") is None


# ---------------- scoring ----------------
def strong():
    return TraderStats(pnl_30d=150_000, pnl_all=2_000_000, volume_30d=1e6, volume_all=1e7, wins=140, settled=200)


def test_strong_trader_is_shark():
    r = score_trader(strong(), S)
    assert r["is_shark"] and r["score"] >= 80 and not r["missing"]


def test_one_lucky_bet_is_not_shark():
    r = score_trader(TraderStats(pnl_30d=500_000, pnl_all=500_000, volume_all=600_000, wins=1, settled=1), S)
    assert not r["eligible"] and not r["is_shark"]


def test_missing_stats_renormalize_and_are_listed():
    r = score_trader(TraderStats(pnl_30d=150_000, pnl_all=2_000_000), S)
    assert set(r["missing"]) == {"win_rate", "sample_size", "roi"}
    assert r["score"] == 100.0  # every available signal at full marks


def test_big_volume_low_roi_scores_lower():
    hi = score_trader(strong(), S)["score"]
    low = strong(); low.volume_all = 1e9; low.volume_30d = 1e8
    assert score_trader(low, S)["score"] < hi


def test_losing_recently_hurts_consistency():
    s = strong(); s.pnl_30d = -10_000
    assert score_trader(s, S)["components"]["consistency"]["mark"] == 0.5


def test_category_fit_uses_category_record():
    sports = score_trader(strong(), S)
    weak = score_trader(TraderStats(pnl_30d=2_000, pnl_all=5_000, volume_all=1e6, wins=100, settled=200), S)
    scores = {"sports": sports, "overall": weak}
    assert score_for_market(scores, "sports", S)["basis"] == "sports"
    off = score_for_market(scores, "politics", S)
    assert off["basis"] == "overall" and off["score"] == round(weak["score"] * S["off_category_penalty"], 1)


def test_settings_clamped():
    s = load_settings({"shark_threshold": 500, "min_position_usd": "abc", "weights": {"roi": -5}})
    assert s["shark_threshold"] == 100
    assert s["min_position_usd"] == DEFAULT_SETTINGS["min_position_usd"]
    assert s["weights"]["roi"] == 0


# ---------------- beliefs ----------------
def _market():
    return Market(platform="polymarket", market_id="m1", title="USC vs Oregon", category="sports", url="",
                  spec=Spec(kind="event"), side_labels=("USC", "Oregon"), outcome_side={"USC": "A", "Oregon": "B"})


def test_hedged_and_small_positions():
    m = _market()
    props, m2p = build_props([m])
    pos = [Position("polymarket", "alice", "m1", "USC", 1000, 0.5, 500, 600),
           Position("polymarket", "bob", "m1", "USC", 200, 0.5, 100, 120),
           Position("polymarket", "bob", "m1", "Oregon", 200, 0.5, 100, 80),
           Position("polymarket", "carol", "m1", "Oregon", 50, 0.5, 25, 20)]   # under $50
    beliefs, skipped = to_beliefs(pos, {"m1": m}, m2p, S)
    assert skipped["too_small"] == 1
    pid = m2p["m1"]
    t = tally(beliefs, {(pid, w): True for w in ("alice", "bob", "carol")})[pid]
    assert t["A"]["count"] == 1 and t["B"]["count"] == 0
    assert [h["trader_id"] for h in t["hedged"]] == ["bob"]


def test_non_sharks_not_counted():
    m = _market()
    props, m2p = build_props([m])
    beliefs, _ = to_beliefs([Position("polymarket", "dave", "m1", "USC", 1000, 0.5, 500, 600)], {"m1": m}, m2p, S)
    assert tally(beliefs, {(m2p["m1"], "dave"): False}) == {}
