"""
Build the rows of the website's main table.

One row per market. A row is either a matched pair (the same market on
Kalshi and Polymarket) or a single-platform market. Only markets with at
least one shark position are kept.
"""

from __future__ import annotations

from typing import Any, Optional

from .matcher import Prop


def _platform_block(prop: Optional[Prop], t: Optional[dict]) -> Optional[dict]:
    if prop is None:
        return None
    t = t or {"A": {"sharks": [], "count": 0, "cost": 0.0, "avg_price": None},
              "B": {"sharks": [], "count": 0, "cost": 0.0, "avg_price": None}, "hedged": []}
    return {
        "prop_id": prop.prop_id,
        "title": prop.title,
        "url": prop.url,
        "price_a": prop.price_a,
        "price_b": None if prop.price_a is None else round(1 - prop.price_a, 4),
        "A": t["A"], "B": t["B"], "hedged": t["hedged"],
    }


def _majority(block: Optional[dict]) -> Optional[str]:
    if not block:
        return None
    a, b = block["A"]["count"], block["B"]["count"]
    if a > b:
        return "A"
    if b > a:
        return "B"
    return None


def make_row(pm: Optional[Prop], k: Optional[Prop], tallies: dict[str, dict],
             match: Optional[dict], settings: dict[str, Any]) -> Optional[dict]:
    base = pm or k
    pmb = _platform_block(pm, tallies.get(pm.prop_id) if pm else None)
    kb = _platform_block(k, tallies.get(k.prop_id) if k else None)

    tot_a = sum(b["A"]["count"] for b in (pmb, kb) if b)
    tot_b = sum(b["B"]["count"] for b in (pmb, kb) if b)
    hedged = sum(len(b["hedged"]) for b in (pmb, kb) if b)
    if tot_a + tot_b == 0 and hedged == 0:
        return None

    cost_a = sum(b["A"]["cost"] for b in (pmb, kb) if b)
    cost_b = sum(b["B"]["cost"] for b in (pmb, kb) if b)
    total = tot_a + tot_b
    agreement = round(100 * max(tot_a, tot_b) / total) if total else None
    minority_share = min(tot_a, tot_b) / total if total else 0

    pm_major, k_major = _majority(pmb), _majority(kb)
    spec = base.spec
    event_date = spec.game_date or spec.deadline or base.end_date

    return {
        "id": (pm.prop_id if pm else "") + "||" + (k.prop_id if k else ""),
        "title": base.title if not (pm and k) else pm.title,
        "kalshi_title": k.title if k else None,
        "category": base.category,
        "event_date": event_date,
        "side_labels": list(base.side_labels),
        "polymarket": pmb,
        "kalshi": kb,
        "total": {"A": tot_a, "B": tot_b},
        "max_side": max(tot_a, tot_b),
        "agreement_pct": agreement,
        "money": {"A": round(cost_a, 2), "B": round(cost_b, 2)},
        "hedged_count": hedged,
        "flags": {
            "split": total >= 2 and minority_share >= settings.get("split_flag_ratio", 0.4),
            "platforms_disagree": bool(pm_major and k_major and pm_major != k_major),
            "both_agree": bool(pm_major and k_major and pm_major == k_major),
        },
        "match": None if not (pm and k) else {
            "confidence": match["confidence"], "reasons": match["reasons"]},
    }


def build_rows(pm_props: dict[str, Prop], k_props: dict[str, Prop],
               matches: dict[str, list[dict]], tallies: dict[str, dict],
               settings: dict[str, Any]) -> list[dict]:
    rows = []
    paired_pm, paired_k = set(), set()
    for m in matches["auto"]:
        row = make_row(pm_props[m["pm"]], k_props[m["kalshi"]], tallies, m, settings)
        paired_pm.add(m["pm"])
        paired_k.add(m["kalshi"])
        if row:
            rows.append(row)
    for pid, p in pm_props.items():
        if pid not in paired_pm:
            row = make_row(p, None, tallies, None, settings)
            if row:
                rows.append(row)
    for kid, k in k_props.items():
        if kid not in paired_k:
            row = make_row(None, k, tallies, None, settings)
            if row:
                rows.append(row)
    # Default sort: most sharks on one side, then most shark money.
    rows.sort(key=lambda r: (r["max_side"], max(r["money"]["A"], r["money"]["B"])), reverse=True)
    return rows
