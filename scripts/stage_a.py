"""Stage a check: real Polymarket data -> scores -> open positions grouped by market."""
import json, logging, sys, collections
sys.path.insert(0, "/home/user/shark-tracker")
from collector.http import PoliteClient
from collector import polymarket as pm
from collector.settings import load_settings
from collector.scoring import score_trader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
S = load_settings({})
c = PoliteClient(cache_dir="/tmp/claude-0/-home-user/eef88090-94d8-56e1-8bb4-e71f8158b0be/scratchpad/cache")
traders = pm.fetch_leaderboards(c, depth=100)
# preliminary score (no history yet) to decide whose history to fetch
prelim = []
for t in traders.values():
    best = max((score_trader(st, S)["score"] for cat in ["overall"] + [x.lower() for x in pm.CATEGORIES]
                if (st := pm.stats_for(t, cat))), default=0)
    prelim.append((best, t))
prelim.sort(key=lambda x: -x[0])
print("unique traders:", len(traders), " prelim>=60:", sum(1 for s, _ in prelim if s >= 60))
top = [t for s, t in prelim[:25]]
out = []
for t in top:
    op = pm.fetch_open_positions(c, t.wallet, min_value=S["min_position_usd"])
    t.history = pm.fetch_history(c, t.wallet)
    scores = {cat: score_trader(st, S) for cat in ["overall"] + [x.lower() for x in pm.CATEGORIES]
              if (st := pm.stats_for(t, cat, len(op)))}
    out.append((t, scores, op))
json.dump([{"name": t.name, "wallet": t.wallet, "scores": sc, "history": t.history,
            "open": op} for t, sc, op in out], open("/tmp/claude-0/-home-user/eef88090-94d8-56e1-8bb4-e71f8158b0be/scratchpad/stage_a.json", "w"))
print("requests:", c.stats)
