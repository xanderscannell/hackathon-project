"""Does the device find what the backend finds? Builds device/src/detect.h for the laptop, replays
drives through it, and compares its hits and windows with pipeline.process() on the same drives.

    pio pkg install -g -t platformio/toolchain-gccmingw32   # once: a laptop g++
    python device/check.py                                  # every drive in data/raw/drives
    python device/check.py drive_20260928_203615
"""
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pipeline  # noqa: E402

HERE = Path(__file__).parent
GXX = Path.home() / ".platformio/packages/toolchain-gccmingw32/bin/g++.exe"
EXE = HERE / "check.exe"


def device(stem):
    out = subprocess.run([str(EXE), str(pipeline.RAW / "drives" / stem)], capture_output=True, text=True, check=True)
    rows = [line.split() for line in out.stdout.splitlines()]
    return ([list(map(float, r[1:])) for r in rows if r[0] == "hit"],
            [list(map(float, r[1:])) for r in rows if r[0] == "win"])


def compare(stem, segs, pieces):
    ref = pipeline.process(stem, segs, pieces)
    hits, wins = device(stem)
    def pair(ref_rows, dev_rows):  # by time, give or take the last rounded digit
        dev = {round(r[0] * 100): r for r in dev_rows}
        return [(r, d) for r in ref_rows
                for d in [next((dev[k] for k in (round(r["t"] * 100) + o for o in (0, -1, 1)) if k in dev), None)] if d]

    both = pair(ref["hits"], hits)
    sev = max((abs(h["severity"] - d[4]) for h, d in both), default=0)
    moved = max((np.hypot(*(np.subtract(pipeline.to_xy(h["lat"], h["lon"]), pipeline.to_xy(d[1], d[2])))) for h, d in both),
                default=0)
    wboth = pair(ref["windows"], wins)
    rn = max((abs(w["rn"] - d[3]) for w, d in wboth), default=0)
    ok = (len(both) == len(ref["hits"]) == len(hits) and len(wboth) == len(ref["windows"]) == len(wins)
          and sev <= 0.06 and moved < 0.5 and rn <= 0.002)
    print(f"{stem}: hits {len(both)} matched of {len(ref['hits'])} backend / {len(hits)} device, "
          f"worst severity diff {sev:.2f}, worst position diff {moved:.2f} m; windows {len(wboth)} matched of "
          f"{len(ref['windows'])} / {len(wins)}, worst rn diff {rn:.4f}  {'ok' if ok else 'MISMATCH'}")
    return ok


def main(stems):
    env = {**os.environ, "PATH": f"{GXX.parent}{os.pathsep}{os.environ['PATH']}"}  # gcc's own DLLs live there
    subprocess.run([str(GXX), "-O2", "-std=c++11", "-static", "-o", str(EXE), str(HERE / "check.cpp")], check=True, env=env)
    stems = stems or sorted(f.name.removesuffix("_imu.csv") for f in (pipeline.RAW / "drives").glob("*_imu.csv"))
    segs, pieces = pipeline.load_segments()
    results = [compare(s, segs, pieces) for s in stems]
    assert all(results), "device and backend disagree"
    print("device matches backend")


if __name__ == "__main__":
    main(sys.argv[1:])
