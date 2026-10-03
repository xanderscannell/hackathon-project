"""Turn logged drives into map-ready JSON.

    python pipeline.py              # every drive in data/raw/drives -> data/out/
    python pipeline.py --selftest   # check the snapping math

Per drive: GPS is interpolated onto the 100 Hz IMU timeline, the moving part
of the drive is cut into fixed-distance windows, each window gets a raw
roughness value and is snapped to the nearest SEMCOG PASER segment running
the same direction.
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
LAT0, LON0 = 42.2, -83.3
KX = np.cos(np.radians(LAT0)) * 111_320
KY = 110_540


def to_xy(lat, lon):
    return (np.asarray(lon) - LON0) * KX, (np.asarray(lat) - LAT0) * KY


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
            "coords": [[round(lat, 5), round(lon, 5)] for lon, lat in coords],
        })
        x, y = to_xy(coords[:, 1], coords[:, 0])
        for k in range(len(x) - 1):
            pieces.append((x[k], y[k], x[k + 1], y[k + 1], i))
    return segs, np.array(pieces)


def snap(px, py, bearing, pieces):
    """Nearest piece within SNAP_M whose direction matches bearing (either way).
    Returns segment index per point, -1 where nothing qualifies."""
    ax, ay, bx, by, seg = pieces.T
    dx, dy = bx - ax, by - ay
    len2 = np.maximum(dx * dx + dy * dy, 1e-9)
    piece_deg = np.degrees(np.arctan2(dx, dy))  # compass-style: 0 = north
    out = np.full(len(px), -1)
    for i in range(len(px)):
        t = np.clip(((px[i] - ax) * dx + (py[i] - ay) * dy) / len2, 0, 1)
        d = np.hypot(ax + t * dx - px[i], ay + t * dy - py[i])
        diff = np.abs((piece_deg - bearing[i] + 90) % 180 - 90)
        d[diff > SNAP_DEG] = np.inf
        k = np.argmin(d)
        if d[k] <= SNAP_M:
            out[i] = int(seg[k])
    return out


def process(stem, segs, pieces):
    imu = pd.read_csv(RAW / "drives" / f"{stem}_imu.csv")
    gps = pd.read_csv(RAW / "drives" / f"{stem}_gps.csv").dropna(subset=["lat", "lon"])
    marks = pd.read_csv(RAW / "drives" / f"{stem}_markers.csv")
    gps = gps.sort_values("t_ms")

    t = imu.t_ms.to_numpy(float)
    lat = np.interp(t, gps.t_ms, gps.lat)
    lon = np.interp(t, gps.t_ms, gps.lon)
    speed = np.interp(t, gps.t_ms, gps.speed_mps)
    # seq is exact 10 ms spacing; it restarts on a board reboot, so clip negative steps
    dt = np.clip(np.diff(imu.seq.to_numpy(), prepend=imu.seq.iloc[0]), 0, 10) * 0.01
    dist = np.cumsum(speed * dt)

    # high-pass: remove slow body motion with a 1 s rolling mean
    def hp(col):
        s = imu[col]
        return (s - s.rolling(100, center=True, min_periods=1).mean()).to_numpy()
    vert, lat_acc = hp("vert"), hp("lat")

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
        windows.append({
            "t": round(t[idx[0]] / 1000, 2),
            "path": [[round(a, 6), round(b, 6)] for a, b in zip(lat[idx][::step], lon[idx][::step])],
            "rough": round(float(np.sqrt(np.mean(vert[idx] ** 2))), 3),
            "rough_lat": round(float(np.sqrt(np.mean(lat_acc[idx] ** 2))), 3),
            "speed": round(float(speed[idx].mean()), 1),
            "_xy": (x.mean(), y.mean(), bearing),
        })

    if windows:
        px, py, br = map(np.array, zip(*(w.pop("_xy") for w in windows)))
        for w, s in zip(windows, snap(px, py, br, pieces)):
            w["seg"] = int(s)
            w["paser"] = segs[s]["paser"] if s >= 0 else None
            w["name"] = segs[s]["name"] if s >= 0 else None

    one_hz = slice(None, None, 100)
    mt = marks.t_ms.to_numpy(float)
    return {
        "id": stem,
        "track": [[round(a, 6), round(b, 6), round(c / 1000, 2)]
                  for a, b, c in zip(lat[one_hz], lon[one_hz], t[one_hz])],
        "windows": windows,
        "markers": [[round(a, 6), round(b, 6), round(c / 1000, 2)]
                    for a, b, c in zip(np.interp(mt, gps.t_ms, gps.lat),
                                       np.interp(mt, gps.t_ms, gps.lon), mt)],
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    segs, pieces = load_segments()
    (OUT / "segments.json").write_text(json.dumps(segs, separators=(",", ":")))
    drives, all_rough = [], []
    for f in sorted((RAW / "drives").glob("*_imu.csv")):
        stem = f.name.removesuffix("_imu.csv")
        d = process(stem, segs, pieces)
        (OUT / f"{stem}.json").write_text(json.dumps(d, separators=(",", ":")))
        rough = [w["rough"] for w in d["windows"]]
        all_rough += rough
        snapped = sum(w["seg"] >= 0 for w in d["windows"])
        print(f"{stem}: {len(d['windows'])} windows, {snapped} on rated segments, "
              f"{len(d['markers'])} marks")
        drives.append(stem)
    q = np.percentile(all_rough, [10, 50, 90]).round(3).tolist()
    (OUT / "index.json").write_text(json.dumps({"drives": drives, "rough_pct": q}))
    print("roughness p10/p50/p90:", q)


def selftest():
    # one east-west piece along y=0 from x=0 to x=100, segment 7
    pieces = np.array([(0, 0, 100, 0, 7)], float)
    px, py = np.array([50, 50, 50, 50]), np.array([5, 5, 30, 5])
    bearing = np.array([90, 270, 90, 0])  # east, west, east but too far, north
    assert snap(px, py, bearing, pieces).tolist() == [7, 7, -1, -1]
    x, y = to_xy([LAT0 + 0.001], [LON0])
    assert abs(y[0] - 110.54) < 0.01 and abs(x[0]) < 1e-9
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
