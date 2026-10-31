"""
One-off: run a refresh using the warmed-up local state (out/state.json),
merged with whatever the live site already has, and upload the result.
Needs SITE_URL and INGEST_KEY in the environment.
"""
import json, logging, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from collector.pipeline import run
from collector.netclient import PoliteClient
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("shark.http").setLevel(logging.WARNING)
from collector.run_remote import Site
site = Site(os.environ["SITE_URL"], os.environ["INGEST_KEY"])
try:
    settings, decisions, remote = site.load()
except Exception:
    settings, decisions, remote = {}, {}, {}
local = json.load(open(os.path.join(os.path.dirname(__file__), "..", "out", "state.json")))
state = dict(local)
state["history"] = {**local.get("history", {}), **remote.get("history", {})}
merged = dict(local.get("positions", {}))
for w, v in remote.get("positions", {}).items():
    if v.get("fetched_at", 0) > merged.get(w, {}).get("fetched_at", 0):
        merged[w] = v
state["positions"] = merged
if remote.get("kalshi"):
    state["kalshi"] = remote["kalshi"]
t0 = time.time()
data, state = run(settings, decisions, state, PoliteClient(),
                  history_seconds=float(sys.argv[1]) if len(sys.argv) > 1 else 75)
json.dump(state, open(os.path.join(os.path.dirname(__file__), "..", "out", "state.json"), "w"))
print("state MB", round(len(json.dumps(state)) / 1e6, 2), "data MB", round(len(json.dumps(data)) / 1e6, 2))
site.save(data, state)
site.report(True, round(time.time() - t0), "seed", data["status"]["polymarket"]["message"])
print("uploaded")
