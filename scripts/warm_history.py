"""
Speed up the one-time history check for many new traders.
Loads the live state, checks trading history for traders that still need it
(best preliminary score first) for N seconds, then merges the results into
the latest live state and uploads it. Website data itself is left alone; the
next scheduled refresh uses the new histories.
Usage: SITE_URL=... INGEST_KEY=... python -m scripts.warm_history 6600
"""
import logging, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from collector import polymarket as pm
from collector.netclient import FetchError, PoliteClient
from collector.pipeline import _score_all
from collector.run_remote import Site
from collector.settings import load_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("shark.http").setLevel(logging.WARNING)
log = logging.getLogger("shark.warm")
seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 3600
site = Site(os.environ["SITE_URL"], os.environ["INGEST_KEY"])
settings, _, state = site.load()
S = load_settings(settings)
c = PoliteClient()
traders = pm.fetch_leaderboards(c, depth=S["leaderboard_depth"])
hist = state.get("history", {})
prelim = sorted(((max((v["score"] for v in _score_all(t, S, None).values()), default=0), t)
                 for t in traders.values() if t.wallet not in hist), key=lambda x: -x[0])
queue = [t for sc, t in prelim if sc >= S["shark_threshold"] - 10]
log.info("%d traders need a history check", len(queue))
new, deadline, last_save = {}, time.time() + seconds, time.time()

def save():
    _, _, live = site.load()          # merge into the newest live state
    live.setdefault("history", {}).update(new)
    site._put("/api/collector/state", live)
    log.info("saved %d new histories (live state now has %d)", len(new), len(live["history"]))

for i, t in enumerate(queue):
    if time.time() > deadline:
        break
    try:
        new[t.wallet] = pm.fetch_history(c, t.wallet)
    except FetchError as e:
        log.warning("history %s: %s", t.name, e)
    if time.time() - last_save > 1200:   # save progress every 20 minutes
        save(); last_save = time.time()
save()
log.info("done: %d checked", len(new))
