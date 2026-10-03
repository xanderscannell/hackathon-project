"""Dashboard + read-only API over data/out, for the map and the Orchestrate agent's tools.

    python backend.py            # http://localhost:8000
    python backend.py --selftest

Locally: index.html, data/out/ and /api/. Public (API_ONLY set, as on Code Engine,
or requests through a Cloudflare tunnel): /api/ only, since data/out holds raw drive tracks.
Nothing else in the repo is ever served. Every miss answers with a
sentence the agent can't skip, never an empty list.
"""
import json
import os
import re
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

OUT = Path(os.environ.get("DATA_DIR", "data/out"))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8000))
API_ONLY = bool(os.environ.get("API_ONLY"))  # set in the Code Engine container: it is public
ALIASES = {"road": "rd", "avenue": "ave", "street": "st", "drive": "dr", "highway": "hwy", "north": "n",
           "south": "s", "east": "e", "west": "w"}
ESTIMATE_NOTE = ("The estimate comes from measured roughness and is within one grade of the rated value on about "
                 "two thirds of held-out sections. Prefer the rated value where one exists.")


def norm(name):
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return " ".join(ALIASES.get(w, w) for w in words)


def load(name):
    return json.loads((OUT / name).read_text())


def pothole_row(p, clock):
    return {"id": p["id"], "road": p["road"] or "unrated road", "state": p["state"], "severity": p["severity"],
            "hit_passes": p["hit_passes"], "passes": p["passes"], "first_seen": p["first_seen"],
            "days_open": p["days_open"], "days_left": clock - p["days_open"], "lat": p["lat"], "lon": p["lon"]}


def potholes(q):
    ph = load("potholes.json")
    road, state = norm(q.get("road", "")), q.get("state", "").strip().lower()
    limit = max(1, min(int(q.get("limit") or 10), 25))
    rows = [p for p in ph["potholes"] if p["state"] != "Suspected" or state == "suspected"]
    if road:
        rows = [p for p in rows if road in norm(p["road"])]
    if state:
        rows = [p for p in rows if p["state"].lower() == state]
    oldest = q.get("order", "").strip().lower() == "oldest"
    if oldest:  # closest to the 30-day mark first; severity breaks ties (rows are already by severity)
        rows = sorted(rows, key=lambda p: -p["days_open"])
    out = {"as_of": ph["as_of"], "clock_days": ph["clock_days"], "total_matching": len(rows),
           "potholes": [pothole_row(p, ph["clock_days"]) for p in rows[:limit]],
           "order": "oldest first (fewest days left)" if oldest else "most severe first"}
    if not rows:
        out["note"] = (f"No {'confirmed ' if state != 'suspected' else ''}potholes match"
                       f"{' road ' + repr(q.get('road')) if road else ''}{' in state ' + state if state else ''}. "
                       "Say there are none in the data; do not make any up.")
    return out


def road(q):
    name = norm(q.get("name", ""))
    if not name:
        return {"matches": [], "note": "No road name given. Ask which road."}
    secs = [s for s in load("sections.json") if name in norm(s["name"])]
    ph = load("potholes.json")
    out = {"as_of": ph["as_of"], "matches": []}
    for rname in sorted({s["name"] for s in secs}):
        rs = [s for s in secs if s["name"] == rname]
        rated = sorted(s["paser"] for s in rs)
        est = sorted(s["est"] for s in rs)
        conf = [p for p in ph["potholes"] if p["road"] == rname and p["state"] != "Suspected"]
        out["matches"].append({
            "road": rname, "half_mile_sections_measured": len(rs),
            "rated_paser_median": rated[len(rated) // 2], "rated_paser_range": [rated[0], rated[-1]],
            "rated_by": "SEMCOG, 2023-2024",
            "estimated_paser_median": est[len(est) // 2], "estimate_note": ESTIMATE_NOTE,
            "confirmed_potholes": len(conf),
            "worst_pothole": pothole_row(conf[0], ph["clock_days"]) if conf else None,
        })
    if not out["matches"]:
        out["note"] = (f"No measured, rated road matches {q.get('name')!r}. There is no rating or estimate for it; "
                       "do not make one up. The vehicle may not have driven it.")
    return out


def work_order(q):
    ph = load("potholes.json")
    try:
        pid = int(q.get("id", ""))
    except ValueError:
        return {"note": "No pothole id given. Find the pothole first, then ask for its work order by id."}
    p = next((p for p in ph["potholes"] if p["id"] == pid), None)
    if p is None:
        return {"note": f"There is no pothole {pid}. Do not make up a work order."}
    lane = ["northbound", "eastbound", "southbound", "westbound"][round(p["heading"] / 90) % 4]
    return {"work_order": f"WO-{p['id']}", **pothole_row(p, ph["clock_days"]), "lane": lane,
            "rated_paser": p["paser"], "history": p["history"],
            "map": f"https://www.google.com/maps?q={p['lat']},{p['lon']}",
            "action": "Inspect and repair. Record the repair date and method.",
            "caveat": "The detection date is a best case: the defect may be older. Triage estimate, not a legal determination."}


def at_risk(q):
    ar = load("at_risk.json")
    name = norm(q.get("road", ""))
    limit = max(1, min(int(q.get("limit") or 10), 25))
    rows = [r for r in ar["roads"] if name in norm(r["road"])] if name else ar["roads"]
    # one sentence per road, so the agent quotes numbers instead of deriving new ones
    rows = [{**r, "summary": f"{r['road']}: {r['likely_poor_miles']} of its {r['fair_good_rated_miles']} fair or good "
             f"rated miles are likely to be poor by {ar['to']}; its riskiest stretch has a "
             f"{round(r['highest_risk'] * 100)}% chance. Rated {r['rating_now']:g} now."} for r in rows]
    out = {"forecast": f"chance of being rated poor (PASER 1-4) by {ar['to']}, from SEMCOG ratings through {ar['from']}",
           "likely_poor_means": f"at least {round(ar['likely_threshold'] * 100)}% chance",
           "track_record": ar["track_record"], "order": "most miles likely poor first",
           "roads": rows[:limit], "total_matching": len(rows)}
    if not rows:
        out["note"] = (f"No fair or good rated road matches {q.get('road')!r}. It may already be rated poor, or not be "
                       "rated at all. Say so; do not estimate a risk.")
    return out


def summary(q):
    return {"facts": load("facts.json"), "note": "Every number here is computed. Quote them; do not total or round them."}


API = {"/api/potholes": potholes, "/api/road": road, "/api/work_order": work_order, "/api/summary": summary,
       "/api/at_risk": at_risk}


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path in API:
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                body, code = API[url.path](q), 200
            except Exception as e:  # the agent gets a sentence, not a stack trace
                body, code = {"note": f"The backend failed ({type(e).__name__}). Say so; do not guess."}, 500
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif API_ONLY or self.headers.get("Cf-Connecting-Ip"):
            # public (Code Engine, or the tunnel): API only. data/out holds raw drive tracks and account ids.
            self.send_error(404)
        elif url.path in ("/", "/index.html") or (url.path.startswith("/data/out/") and ".." not in url.path):
            super().do_GET()
        else:
            self.send_error(404)


def selftest():
    assert norm("Michigan Avenue") == norm("michigan ave") == "michigan ave"
    assert "note" in potholes({"road": "No Such Road"})
    assert "note" in road({"name": "Martinsville Road"}) and not road({"name": "Martinsville Road"})["matches"]
    m = road({"name": "michigan avenue"})["matches"]
    assert m and m[0]["road"] == "Michigan Ave" and m[0]["confirmed_potholes"] > 0
    assert "note" in work_order({"id": "abc"}) and "note" in work_order({"id": "999999"})
    old = potholes({"order": "oldest"})["potholes"]
    assert old[0]["days_open"] == max(p["days_open"] for p in potholes({"limit": 25})["potholes"] + old)
    risk = at_risk({})["roads"]
    assert risk and all(a["likely_poor_miles"] >= b["likely_poor_miles"] for a, b in zip(risk, risk[1:]))
    assert at_risk({"road": "michigan avenue"})["roads"][0]["road"] == "Michigan Ave"
    assert "note" in at_risk({"road": "No Such Road"})
    first = potholes({})["potholes"][0]
    assert work_order({"id": str(first["id"])})["work_order"] == f"WO-{first['id']}"
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print(f"http://{HOST}:{PORT}")
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
