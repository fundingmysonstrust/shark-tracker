"""Run one refresh locally, reading/writing out/state.json and out/data.json."""
import json, logging, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from collector.pipeline import run
from collector.http import PoliteClient
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("shark.http").setLevel(logging.WARNING)
out = os.path.join(os.path.dirname(__file__), "..", "out")
os.makedirs(out, exist_ok=True)
sp = os.path.join(out, "state.json")
state = json.load(open(sp)) if os.path.exists(sp) else {}
hs = float(sys.argv[1]) if len(sys.argv) > 1 else 75
ps = float(sys.argv[2]) if len(sys.argv) > 2 else 170
data, state = run({}, {}, state, PoliteClient(), history_seconds=hs, positions_seconds=ps)
json.dump(state, open(sp, "w"))
json.dump(data, open(os.path.join(out, "data.json"), "w"))
print(data["status"])
