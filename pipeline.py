"""Turn logged drives into map-ready JSON.

    python pipeline.py              # every drive in data/raw/drives -> data/out/
    python pipeline.py --selftest   # check the snapping and hit math

Per drive: GPS is interpolated onto the 100 Hz IMU timeline, the moving part
of the drive is cut into fixed-distance windows, each window gets a
speed-normalized roughness and is snapped to the nearest SEMCOG PASER segment
running the same direction, and severe hits are picked out of the raw signal.

Across drives: windows on rated roads are grouped into half-mile sections,
roughness is mapped to an estimated PASER grade with one line fitted on the
earlier drives, and the fit is scored on the later drives.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

RAW = Path("data/raw")
OUT = Path("data/out")
WINDOW_M = 50          # roughness window length along the road
MIN_SPEED = 3.0        # m/s; below this the vehicle counts as stopped
SNAP_M = 20            # max distance from a window to a PASER segment
SNAP_DEG = 35          # max heading difference between window and segment
SPEED_EXP = 0.31       # roughness ~ speed^0.31, fitted within segments across passes
REF_SPEED = 20         # m/s; roughness is reported as if driven at this speed
HIT_MS2 = 4.5          # severe hit: speed-normalized peak above this...
HIT_RATIO = 3          # ...and this many times the surrounding road's level
SECTION_MI = 0.5       # rated sections are this long along a road
TEST_PREFIX = "drive_202610"  # drives scored, never fitted on
CLUSTER_M = 15         # hits this close, same direction of travel, are one spot (GPS ~4 m)
REPAIR_PASSES = 3      # clean passes in a row before a confirmed spot counts as repaired
WORSE_RATIO = 1.3      # a hit this much above the spot's median severity so far is worsening
CLOCK_DAYS = 30        # MCL 691.1403: knowledge presumed after 30 days readily apparent
# Rated before a rebuild; their ratings would teach the fit the wrong answer.
STALE = {"Evergreen Rd"}  # ponytail: whole road by name; the rebuilt stretch only, if other parts matter
LAT0, LON0 = 42.2, -83.3
KX = np.cos(np.radians(LAT0)) * 111_320
KY = 110_540


def to_xy(lat, lon):
    return (np.asarray(lon) - LON0) * KX, (np.asarray(lat) - LAT0) * KY


def from_xy(x, y):
    return np.asarray(y) / KY + LAT0, np.asarray(x) / KX + LON0


def load_segments():
    feats = json.loads((RAW / "semcog_paser_commute.geojson").read_text())["features"]
    segs, pieces = [], []
    for i, f in enumerate(feats):
        p = f["properties"]
        coords = np.array(f["geometry"]["coordinates"])
        segs.append({
            "id": i,
            "name": p["STRNAME"].strip(),
            "paser": p["AS_OF_24"],
            "evalyear": p["EVALYEAR"],
            "surface": p["SURFACE"],
            "section": f"{p['PR']}|{p['AS_OF_24']}|{int(p['BMP'] // SECTION_MI)}",
            "coords": [[round(lat, 5), round(lon, 5)] for lon, lat in coords],
        })
        x, y = to_xy(coords[:, 1], coords[:, 0])
        for k in range(len(x) - 1):
            pieces.append((x[k], y[k], x[k + 1], y[k + 1], i))
    return segs, np.array(pieces)


def angle_diff(a, b, directed=False):
    """Smallest difference between compass bearings; undirected treats a road both ways."""
    m = 360 if directed else 180
    return np.abs((np.asarray(a) - b + m / 2) % m - m / 2)


def snap(px, py, bearing, pieces, radius=SNAP_M, directed=False):
    """Nearest piece within radius whose direction matches bearing (either way
    unless directed). Returns segment index per point, -1 where nothing qualifies."""
    ax, ay, bx, by, seg = pieces.T
    dx, dy = bx - ax, by - ay
    len2 = np.maximum(dx * dx + dy * dy, 1e-9)
    piece_deg = np.degrees(np.arctan2(dx, dy))  # compass-style: 0 = north
    out = np.full(len(px), -1)
    for i in range(len(px)):
        t = np.clip(((px[i] - ax) * dx + (py[i] - ay) * dy) / len2, 0, 1)
        d = np.hypot(ax + t * dx - px[i], ay + t * dy - py[i])
        d[angle_diff(piece_deg, bearing[i], directed) > SNAP_DEG] = np.inf
        k = np.argmin(d)
        if d[k] <= radius:
            out[i] = int(seg[k])
    return out


def speed_norm(x, speed):
    return x * (REF_SPEED / np.maximum(speed, MIN_SPEED)) ** SPEED_EXP


def find_hits(t, speed, mag):
    """Start index of each severe hit. mag is high-passed vertical+lateral acceleration.
    A hit stands out from the surrounding ~5 s of road, lasts at least 3 samples
    within 0.1 s (a lone sample is never a hit), and only counts while moving.
    Samples less than 1 s apart belong to the same hit."""
    background = pd.Series(mag).rolling(500, center=True, min_periods=1).median().to_numpy()
    cand = (speed >= MIN_SPEED) & (speed_norm(mag, speed) > HIT_MS2) & (mag > HIT_RATIO * background)
    dense = pd.Series(cand.astype(int)).rolling(10, center=True, min_periods=1).sum().to_numpy() >= 3
    starts = []
    for i in np.flatnonzero(cand & dense):
        if not starts or t[i] - t[starts[-1]] > 1000:
            starts.append(i)
    return starts


def process(stem, segs, pieces):
    imu = pd.read_csv(RAW / "drives" / f"{stem}_imu.csv")
    gps = pd.read_csv(RAW / "drives" / f"{stem}_gps.csv").dropna(subset=["lat", "lon"])
    gps = gps.sort_values("t_ms")
    mt = pd.read_csv(RAW / "drives" / f"{stem}_markers.csv").t_ms.to_numpy(float)

    t = imu.t_ms.to_numpy(float)
    lat = np.interp(t, gps.t_ms, gps.lat)
    lon = np.interp(t, gps.t_ms, gps.lon)
    speed = np.interp(t, gps.t_ms, gps.speed_mps)
    # seq is exact 10 ms spacing; it restarts on a board reboot, so clip negative steps
    dt = np.clip(np.diff(imu.seq.to_numpy(), prepend=imu.seq.iloc[0]), 0, 10) * 0.01
    dist = np.cumsum(speed * dt)

    # high-pass: remove slow body motion and steady cornering with a 1 s rolling mean
    def hp(col):
        s = imu[col]
        return (s - s.rolling(100, center=True, min_periods=1).mean()).to_numpy()
    vert, lat_acc = hp("vert"), hp("lat")
    mag = np.hypot(vert, lat_acc)

    moving = speed >= MIN_SPEED
    win = (dist // WINDOW_M).astype(int)
    windows = []
    for w in np.unique(win[moving]):
        idx = np.flatnonzero((win == w) & moving)
        if len(idx) < 100:  # under 1 s of moving data
            continue
        x, y = to_xy(lat[idx], lon[idx])
        bearing = np.degrees(np.arctan2(x[-1] - x[0], y[-1] - y[0]))
        step = max(1, len(idx) // 8)
        rough = float(np.sqrt(np.mean(vert[idx] ** 2)))
        v = float(speed[idx].mean())
        windows.append({
            "t": round(t[idx[0]] / 1000, 2),
            "path": [[round(a, 6), round(b, 6)] for a, b in zip(lat[idx][::step], lon[idx][::step])],
            "rough": round(rough, 3),
            "rough_lat": round(float(np.sqrt(np.mean(lat_acc[idx] ** 2))), 3),
            "rn": round(float(speed_norm(rough, v)), 3),
            "speed": round(v, 1),
            "_xy": (x.mean(), y.mean(), bearing),
        })

    if windows:
        px, py, br = map(np.array, zip(*(w.pop("_xy") for w in windows)))
        for w, s in zip(windows, snap(px, py, br, pieces)):
            w["seg"] = int(s)
            w["paser"] = segs[s]["paser"] if s >= 0 else None
            w["name"] = segs[s]["name"] if s >= 0 else None

    hits = []
    for i in find_hits(t, speed, mag):
        j = slice(i, i + 100)  # the hit's first second
        a, b = max(i - 100, 0), min(i + 100, len(t) - 1)  # heading over the 2 s around it
        x, y = to_xy(lat[[a, b]], lon[[a, b]])
        hits.append({
            "t": round(t[i] / 1000, 2),
            "lat": round(lat[i], 6), "lon": round(lon[i], 6),
            "heading": round(float(np.degrees(np.arctan2(x[1] - x[0], y[1] - y[0]))) % 360, 1),
            "severity": round(float(speed_norm(mag[j].max(), speed[i])), 1),
            "vert": round(float(np.abs(vert[j]).max()), 1),
            "lat_g": round(float(np.abs(lat_acc[j]).max()), 1),
            "speed": round(float(speed[i]), 1),
        })

    one_hz = slice(None, None, 100)
    return {
        "id": stem,
        "track": [[round(a, 6), round(b, 6), round(c / 1000, 2), round(s, 1)]
                  for a, b, c, s in zip(lat[one_hz], lon[one_hz], t[one_hz], speed[one_hz])],
        "windows": windows,
        "hits": hits,
        "markers": [[round(a, 6), round(b, 6), round(c / 1000, 2)]
                    for a, b, c in zip(np.interp(mt, gps.t_ms, gps.lat),
                                       np.interp(mt, gps.t_ms, gps.lon), mt)],
    }


def fit_paser(rn, paser, n):
    """One line: PASER = a + b * ln(roughness), weighted by windows per section."""
    b, a = np.polyfit(np.log(rn), paser, 1, w=np.sqrt(n))
    return a, b


def estimate(rn, a, b):
    return np.clip(np.round(a + b * np.log(rn)), 1, 10).astype(int)


def sections_table(drives, segs):
    rows = [(d["id"], segs[w["seg"]]["section"], w["seg"], w["rn"], w["paser"])
            for d in drives for w in d["windows"]
            if w["seg"] >= 0 and segs[w["seg"]]["name"] not in STALE]
    return pd.DataFrame(rows, columns=["drive", "section", "seg", "rn", "paser"])


def summarize(df):
    g = df.groupby("section")
    s = g.agg(rn=("rn", "median"), paser=("paser", "first"), n=("rn", "size"),
              passes=("drive", "nunique"))
    return s[s.n >= 4]  # under 200 m of road in total is too little to rate


def drive_date(drive_id):
    return pd.Timestamp(drive_id.split("_")[1]).date()


def cluster_hits(drives):
    """Group hits from every drive that land within CLUSTER_M of each other while
    travelling the same way, so opposite lanes stay separate."""
    clusters = []
    for d in drives:
        for h in d["hits"]:
            x, y = to_xy(h["lat"], h["lon"])
            best, best_d = None, CLUSTER_M
            for c in clusters:
                dist = np.hypot(c["x"] - x, c["y"] - y)
                if dist <= best_d and angle_diff(c["heading"], h["heading"], directed=True) <= SNAP_DEG:
                    best, best_d = c, dist
            if best is None:
                best = {"x": 0.0, "y": 0.0, "heading": h["heading"], "hits": []}
                clusters.append(best)
            best["hits"].append((d["id"], h))
            n = len(best["hits"])
            best["x"] += (x - best["x"]) / n  # running mean position
            best["y"] += (y - best["y"]) / n
    return clusters


def lifecycle(passes):
    """State after a time-ordered list of (hit, severity) passes over one spot."""
    state, clean, severities = None, 0, []
    for hit, sev in passes:
        if not hit:
            clean += 1
            if state in ("Confirmed", "Worsening", "PatchFailed") and clean >= REPAIR_PASSES:
                state = "Repaired"
            continue
        clean = 0
        if state is None:
            state = "Suspected"
        elif state == "Suspected":
            state = "Confirmed"
        elif state == "Repaired":
            state = "PatchFailed"
        elif sev > WORSE_RATIO * np.median(severities):
            state = "Worsening"
        severities.append(sev)
    return state


def build_potholes(drives, segs, pieces):
    """Confirm hits across passes and give every spot a lifecycle and a clock.
    Day 0 is the first detection: the earliest evidence it existed."""
    clusters = cluster_hits(drives)
    as_of = max(drive_date(d["id"]) for d in drives)
    # which clusters did each drive go over, in the same direction, while moving?
    px = np.array([c["x"] for c in clusters])
    py = np.array([c["y"] for c in clusters])
    hd = np.array([c["heading"] for c in clusters])
    passed = {}
    for d in drives:
        tr = np.array(d["track"])
        x, y = to_xy(tr[:, 0], tr[:, 1])
        moving = (tr[:-1, 3] >= MIN_SPEED) & (tr[1:, 3] >= MIN_SPEED)
        track_pieces = np.column_stack([x[:-1], y[:-1], x[1:], y[1:], np.zeros(len(x) - 1)])[moving]
        passed[d["id"]] = snap(px, py, hd, track_pieces, radius=CLUSTER_M, directed=True) >= 0

    lat, lon = from_xy(px, py)
    road = snap(px, py, hd, pieces)
    out = []
    for i, c in enumerate(clusters):
        by_drive = {}
        for drive_id, h in c["hits"]:
            by_drive.setdefault(drive_id, []).append(h["severity"])
        history = []
        for d in drives:  # drives are in time order
            if d["id"] in by_drive or passed[d["id"]][i]:
                sev = max(by_drive.get(d["id"], [0]))
                history.append({"drive": d["id"], "date": str(drive_date(d["id"])),
                                "hit": d["id"] in by_drive, "severity": sev})
        hit_passes = [p for p in history if p["hit"]]
        first = drive_date(hit_passes[0]["drive"])
        s = int(road[i])
        out.append({
            "id": i,
            "lat": round(float(lat[i]), 6), "lon": round(float(lon[i]), 6),
            "heading": round(float(c["heading"]), 1),
            "road": segs[s]["name"] if s >= 0 else None,
            "paser": segs[s]["paser"] if s >= 0 else None,
            "state": lifecycle([(p["hit"], p["severity"]) for p in history]),
            "severity": round(float(np.median([p["severity"] for p in hit_passes])), 1),
            "passes": len(history), "hit_passes": len(hit_passes),
            "confidence": round(len(hit_passes) / len(history), 2),
            "first_seen": str(first), "last_hit": hit_passes[-1]["date"],
            "days_open": (as_of - first).days,
            "history": history,
        })
    # ranked list: severity decides order; the clock is shown, not used to sort
    out.sort(key=lambda p: -p["severity"])
    return {"as_of": str(as_of), "clock_days": CLOCK_DAYS, "potholes": out}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    segs, pieces = load_segments()
    drives = []
    for f in sorted((RAW / "drives").glob("*_imu.csv")):
        d = process(f.name.removesuffix("_imu.csv"), segs, pieces)
        drives.append(d)

    # fit on the earlier drives, score on the later ones
    df = sections_table(drives, segs)
    test = df.drive.str.startswith(TEST_PREFIX)
    train_s, test_s = summarize(df[~test]), summarize(df[test])
    a, b = fit_paser(train_s.rn, train_s.paser, train_s.n)
    err = np.abs(estimate(test_s.rn, a, b) - test_s.paser)
    guess = round(np.average(train_s.paser, weights=train_s.n))
    guess_err = np.abs(guess - test_s.paser)
    est_test = estimate(test_s.rn, a, b)
    accuracy = {
        "sections": len(test_s),
        "within1": round(float((err <= 1).mean()), 3),
        "exact": round(float((err == 0).mean()), 3),
        "mae": round(float(err.mean()), 2),
        "guess": int(guess),
        "guess_within1": round(float((guess_err <= 1).mean()), 3),
        "guess_mae": round(float(guess_err.mean()), 2),
        "corr": round(float(np.corrcoef(np.log(test_s.rn), test_s.paser)[0, 1]), 2),
        "pairs": [[int(p), int(e)] for p, e in zip(test_s.paser, est_test)],
        "fit": [round(a, 3), round(b, 3)],
    }

    # every window gets an estimate, rated road or not
    for d in drives:
        for w in d["windows"]:
            w["est"] = int(estimate(w["rn"], a, b))

    # sections for the map: all passes pooled
    all_s = summarize(df)
    segs_by_section = df.groupby("section").seg.unique()
    sections = [{
        "name": segs[segs_by_section[k][0]]["name"],
        "paser": int(r.paser), "est": int(estimate(r.rn, a, b)),
        "rn": round(float(r.rn), 3), "n": int(r.n), "passes": int(r.passes),
        "coords": [segs[s]["coords"] for s in segs_by_section[k]],
    } for k, r in all_s.iterrows()]

    for d in drives:
        (OUT / f"{d['id']}.json").write_text(json.dumps(d, separators=(",", ":")))
        snapped = sum(w["seg"] >= 0 for w in d["windows"])
        print(f"{d['id']}: {len(d['windows'])} windows, {snapped} on rated segments, "
              f"{len(d['hits'])} hits, {len(d['markers'])} marks")
    (OUT / "segments.json").write_text(json.dumps(segs, separators=(",", ":")))
    (OUT / "sections.json").write_text(json.dumps(sections, separators=(",", ":")))
    rn_all = [w["rn"] for d in drives for w in d["windows"]]
    (OUT / "index.json").write_text(json.dumps({
        "drives": [d["id"] for d in drives],
        "rough_pct": np.percentile(rn_all, [10, 50, 90]).round(3).tolist(),
        "accuracy": accuracy,
    }))
    potholes = build_potholes(drives, segs, pieces)
    (OUT / "potholes.json").write_text(json.dumps(potholes, separators=(",", ":")))
    states = pd.Series([p["state"] for p in potholes["potholes"]]).value_counts().to_dict()
    print(f"{len(potholes['potholes'])} spots as of {potholes['as_of']}: {states}")
    print(f"fit PASER = {a:.2f} {b:+.2f} ln(roughness) on {len(train_s)} sections")
    print(f"test: {accuracy['sections']} sections, within one grade {accuracy['within1']:.0%}, "
          f"MAE {accuracy['mae']}; always guessing {guess}: {accuracy['guess_within1']:.0%}, "
          f"MAE {accuracy['guess_mae']}")


def selftest():
    # one east-west piece along y=0 from x=0 to x=100, segment 7
    pieces = np.array([(0, 0, 100, 0, 7)], float)
    px, py = np.array([50, 50, 50, 50]), np.array([5, 5, 30, 5])
    bearing = np.array([90, 270, 90, 0])  # east, west, east but too far, north
    assert snap(px, py, bearing, pieces).tolist() == [7, 7, -1, -1]
    x, y = to_xy([LAT0 + 0.001], [LON0])
    assert abs(y[0] - 110.54) < 0.01 and abs(x[0]) < 1e-9
    la, lo = from_xy(*to_xy(42.1155, -83.3919))
    assert abs(la - 42.1155) < 1e-9 and abs(lo + 83.3919) < 1e-9

    # 20 s of quiet road at 20 m/s with one 5-sample bump and one lone spike
    t = np.arange(2000) * 10.0
    speed = np.full(2000, 20.0)
    mag = np.full(2000, 0.5)
    mag[800:805] = 8.0   # real hit
    mag[1500] = 30.0     # lone corrupted sample: never a hit
    assert find_hits(t, speed, mag) == [800]
    assert find_hits(t, np.zeros(2000), mag) == []  # parked: nothing counts

    H, M = (True, 5.0), (False, 0)
    assert lifecycle([M, H]) == "Suspected"
    assert lifecycle([H, M, H]) == "Confirmed"
    assert lifecycle([H, H, (True, 9.0)]) == "Worsening"
    assert lifecycle([H, H, M, M]) == "Confirmed"          # two misses is not a repair
    assert lifecycle([H, H, M, M, M]) == "Repaired"
    assert lifecycle([H, H, M, M, M, H]) == "PatchFailed"
    assert lifecycle([H, M, M, M]) == "Suspected"          # never confirmed, never repaired
    assert angle_diff(10, 350, directed=True) == 20 and angle_diff(10, 190) == 0
    assert angle_diff(10, 190, directed=True) == 180       # opposite lanes stay apart
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
