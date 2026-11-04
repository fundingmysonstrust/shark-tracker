"""
Recommended multi-leg combos, built from shark consensus, with live returns.

How combos are built (once a day per category and style, so their returns
can be tracked honestly from the moment they were listed):

  * A "leg" is one side of one open market where at least `MIN_SHARKS`
    sharks hold a position and at least `MIN_AGREE`% of them are on that
    side. Its price is that side's current Polymarket price.
  * Legs must come from different events (two markets about the same race
    move together, which would overstate the payout).
  * Two styles per category:
      - "consensus":  the 3 legs with the strongest shark agreement
                      (leg price 50-93¢, so each leg still pays something).
      - "payout":     the 3 strongest-agreement legs priced 25-65¢, for a
                      bigger combined payout.
  * Combined price = product of the leg prices (treats legs as independent).
    Payout multiple = 1 / combined price.

Live returns: for a $1 stake at listing, the combo is worth
current combined price / listed combined price. A leg that settles on the
chosen side counts as 1.00, a lost leg makes the combo worth 0. A combo is
closed when all legs settle, any leg loses, or after MAX_DAYS.

Prices come from gamma-api /markets?condition_ids=... (20 per request).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Optional

from .matcher import event_tokens, jaccard
from .netclient import FetchError, PoliteClient

log = logging.getLogger("shark.combos")
GAMMA = "https://gamma-api.polymarket.com"

MIN_SHARKS = 3
MIN_AGREE = 80
LEGS = 3
MAX_DAYS = 75   # also the furthest a leg may settle from listing
STYLES = {
    "consensus": {"lo": 0.50, "hi": 0.93, "label": "Highest agreement"},
    "payout": {"lo": 0.25, "hi": 0.65, "label": "Bigger payout"},
}
DAY = 86400


def _leg_from_row(r: dict) -> Optional[dict]:
    a, b = r["total"]["A"], r["total"]["B"]
    n = a + b
    if n < MIN_SHARKS or (r.get("agreement_pct") or 0) < MIN_AGREE:
        return None
    side = "A" if a >= b else "B"
    pm = r["polymarket"]
    price = pm["price_a"] if side == "A" else pm["price_b"]
    if price is None:
        return None
    return {
        "market_id": r["market_id"], "title": r["title"], "category": r["category"],
        "event": r.get("event_title") or r["title"], "end_date": r.get("end_date"),
        "outcome_index": 0 if side == "A" else 1, "label": r["side_labels"][0 if side == "A" else 1],
        "sharks": max(a, b), "of": n, "agree": r["agreement_pct"], "money": r["money"][side],
        "price": round(float(price), 4), "url": pm.get("url"),
    }


# Words that don't identify a subject: sharing them doesn't make two markets related.
GENERIC = set("""reach dip hit win wins win. before after race election elections price market cap highest lowest new next
first above below between january february march april may june july august september october november december
final deal game games match winner winners nominee nomination party candidate seat seats vs total over under yes no
""".split())


def _related(a: dict, b: dict) -> bool:
    """Legs about the same subject move together (e.g. two Bitcoin price markets)."""
    ta = event_tokens(a["title"] + " " + a["event"]) - GENERIC
    tb = event_tokens(b["title"] + " " + b["event"]) - GENERIC
    return a["event"] == b["event"] or bool(ta & tb)


def _pick(legs: list[dict], lo: float, hi: float) -> list[dict]:
    """Strongest-agreement legs in the price band, none related to each other."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    horizon = time.strftime("%Y-%m-%d", time.gmtime(time.time() + MAX_DAYS * DAY))
    cands = [l for l in legs if lo <= l["price"] <= hi and today <= (l.get("end_date") or "9999") <= horizon]
    cands.sort(key=lambda l: (l["agree"], l["sharks"], l["money"]), reverse=True)
    out = []
    for l in cands:
        if any(_related(l, o) for o in out):
            continue
        out.append(l)
        if len(out) == LEGS:
            break
    return out if len(out) == LEGS else []


def _cid(legs: list[dict]) -> str:
    key = "|".join(sorted(f"{l['market_id']}:{l['outcome_index']}" for l in legs))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def _prices(client: PoliteClient, cids: list[str]) -> dict[str, dict]:
    """condition id -> {"prices": [p0, p1], "closed": bool}"""
    out: dict[str, dict] = {}
    cids = sorted(set(cids))
    for i in range(0, len(cids), 20):
        try:
            rows = client.get_json(f"{GAMMA}/markets", {"condition_ids": cids[i:i + 20]}) or []
        except FetchError as e:
            log.warning("combo prices: %s", e)
            continue
        for m in rows:
            try:
                prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
            except (ValueError, TypeError):
                continue
            out[m.get("conditionId")] = {"prices": prices, "closed": bool(m.get("closed"))}
    return out


def update(client: PoliteClient, rows: list[dict], cstate: Optional[dict]) -> tuple[dict, dict]:
    """Create today's combos where needed, refresh live prices, return (data, state)."""
    now = int(time.time())
    cstate = cstate or {}
    combos: list[dict] = cstate.get("combos", [])

    # 1. new combos: one per category+style per day, only if the legs changed
    legs_by_cat: dict[str, list[dict]] = {}
    for r in rows:
        leg = _leg_from_row(r)
        if leg:
            legs_by_cat.setdefault(leg["category"], []).append(leg)
    # Use live prices (and drop settled/decided markets) before choosing legs.
    all_legs = [l for ls in legs_by_cat.values() for l in ls]
    live0 = _prices(client, [l["market_id"] for l in all_legs])
    for cat in list(legs_by_cat):
        keep = []
        for l in legs_by_cat[cat]:
            info = live0.get(l["market_id"])
            if not info or info["closed"] or len(info["prices"]) <= l["outcome_index"]:
                continue
            p = info["prices"][l["outcome_index"]]
            if not (0.02 <= p <= 0.98):
                continue
            keep.append({**l, "price": round(p, 4)})
        legs_by_cat[cat] = keep
    for cat, legs in legs_by_cat.items():
        for style, cfg in STYLES.items():
            recent = [c for c in combos if c["category"] == cat and c["style"] == style and now - c["created_at"] < DAY]
            if recent:
                continue
            pick = _pick(legs, cfg["lo"], cfg["hi"])
            if not pick:
                continue
            cid = _cid(pick)
            if any(c["id"] == cid and c["status"] == "open" for c in combos):
                continue
            price = 1.0
            for l in pick:
                price *= l["price"]
            combos.append({
                "id": cid, "category": cat, "style": style, "style_label": cfg["label"],
                "created_at": now, "status": "open",
                "entry_price": round(price, 6),
                "legs": [{**l, "entry_price": l["price"], "now": l["price"], "result": None} for l in pick],
            })

    # 2. live prices and settlement for open combos
    open_c = [c for c in combos if c["status"] == "open"]
    live = _prices(client, [l["market_id"] for c in open_c for l in c["legs"]])
    for c in open_c:
        fresh = c["created_at"] == now
        cur = 1.0
        for l in c["legs"]:
            info = live.get(l["market_id"])
            if info and len(info["prices"]) > l["outcome_index"]:
                p = info["prices"][l["outcome_index"]]
                l["now"] = round(p, 4)
                if fresh:  # listing price = the live price at the moment of listing
                    l["entry_price"] = l["price"] = l["now"]
                if info["closed"]:
                    l["result"] = "won" if p >= 0.99 else "lost" if p <= 0.01 else None
            cur *= 1.0 if l["result"] == "won" else 0.0 if l["result"] == "lost" else l["now"]
        if fresh:
            entry = 1.0
            for l in c["legs"]:
                entry *= l["entry_price"]
            c["entry_price"] = round(entry, 6)
        c["now_price"] = round(cur, 6)
        c["return_pct"] = round(100 * (cur / c["entry_price"] - 1), 1) if c["entry_price"] else None
        c["updated_at"] = now
        if any(l["result"] == "lost" for l in c["legs"]):
            c.update(status="lost", closed_at=now, return_pct=-100.0)
        elif all(l["result"] == "won" for l in c["legs"]):
            c.update(status="won", closed_at=now, return_pct=round(100 * (1 / c["entry_price"] - 1), 1))
        elif now - c["created_at"] > MAX_DAYS * DAY:
            c.update(status="expired", closed_at=now)

    # keep all open ones and the 200 most recent closed ones (track record)
    closed = sorted([c for c in combos if c["status"] != "open"], key=lambda c: c.get("closed_at", 0), reverse=True)[:200]
    combos = [c for c in combos if c["status"] == "open"] + closed

    settled = [c for c in closed if c["status"] in ("won", "lost")]
    record = {
        "settled": len(settled), "won": sum(1 for c in settled if c["status"] == "won"),
        "avg_return_pct": round(sum(c["return_pct"] for c in settled) / len(settled), 1) if settled else None,
        "open": sum(1 for c in combos if c["status"] == "open"),
    }
    data = {"combos": combos, "record": record, "updated_at": now,
            "rules": {"min_sharks": MIN_SHARKS, "min_agree": MIN_AGREE, "legs": LEGS, "max_days": MAX_DAYS}}
    return data, {"combos": combos}
