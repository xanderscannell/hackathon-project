"""Score every way of estimating PASER on the same held-out drives.

    .venv-ml/Scripts/python tspulse_embed.py   # first: IBM TSPulse embeddings
    python model.py                            # -> data/out/models.json

Fitted on the Sep drives, scored on half-mile sections from the Oct 1 drives
(TEST_PREFIX). Same sections for every row, so the rows compare directly.
"""
import json
import warnings

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, spearmanr
from sklearn.decomposition import PCA
from sklearn.linear_model import RidgeCV
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

import pipeline as P

BANDS = [(0.5, 2), (2, 5), (5, 10), (10, 20), (20, 50)]
TSP_DIMS = 5  # principal components of the 3312-dim TSPulse embedding


def window_features(stem, windows):
    """Hand-built features for the same 50 m windows pipeline.py made."""
    imu = pd.read_csv(P.RAW / "drives" / f"{stem}_imu.csv")
    gps = pd.read_csv(P.RAW / "drives" / f"{stem}_gps.csv").dropna(subset=["lat"]).sort_values("t_ms")
    t = imu.t_ms.to_numpy(float)
    speed = np.interp(t, gps.t_ms, gps.speed_mps)
    dt = np.clip(np.diff(imu.seq.to_numpy(), prepend=imu.seq.iloc[0]), 0, 10) * 0.01
    win = (np.cumsum(speed * dt) // P.WINDOW_M).astype(int)
    hp = lambda c: (imu[c] - imu[c].rolling(100, center=True, min_periods=1).mean()).to_numpy()
    v, sig = hp("vert"), {c: hp(c) for c in ["lat", "fwd", "gx", "gy", "gz"]}
    moving = speed >= P.MIN_SPEED
    rows = []
    for w in np.unique(win[moving]):
        idx = np.flatnonzero((win == w) & moving)
        if len(idx) < 100:
            continue
        x = v[idx]
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
        fr = np.fft.rfftfreq(len(x), 0.01)
        total = spec[fr >= 0.5].sum() + 1e-9
        r = {f"band_{lo}_{hi}": spec[(fr >= lo) & (fr < hi)].sum() / total for lo, hi in BANDS}
        r |= {f"{c}_rms": np.sqrt(np.mean(s[idx] ** 2)) for c, s in sig.items()}
        r |= {"v_peak": np.abs(x).max(), "v_p95": np.percentile(np.abs(x), 95), "v_kurt": kurtosis(x)}
        rows.append(r)
    f = pd.DataFrame(rows)
    assert len(f) == len(windows), stem  # same windowing as pipeline.py
    return f


def main():
    warnings.filterwarnings("ignore")
    index = json.loads((P.OUT / "index.json").read_text())
    segs, _ = P.load_segments()
    frames = []
    for stem in index["drives"]:
        d = json.loads((P.OUT / f"{stem}.json").read_text())
        w = pd.DataFrame(d["windows"])
        f = window_features(stem, w)
        hit_t = np.array([h["t"] for h in d["hits"]])
        edges = np.append(w.t.to_numpy(), np.inf)
        f["hits"] = [((hit_t >= edges[i]) & (hit_t < edges[i + 1])).sum() for i in range(len(w))]
        z = np.load(P.OUT / "tspulse" / f"{stem}.npz")
        mid = w.t.to_numpy() + P.WINDOW_M / 2 / w.speed.to_numpy()
        f["emb"] = list(z["emb"].astype(np.float32)[np.abs(z["t"][None] - mid[:, None]).argmin(1)])
        frames.append(f.assign(drive=stem, seg=w.seg, paser=w.paser, rn=w.rn))
    df = pd.concat(frames, ignore_index=True)
    df = df[(df.seg >= 0) & [segs[s]["name"] not in P.STALE for s in df.seg.clip(0)]]
    df["section"] = [segs[s]["section"] for s in df.seg]
    feats = [c for c in df.columns if c.startswith(("band_", "v_")) or c.endswith("_rms")]

    def sections(x):
        g = x.groupby("section")
        s = np.log(g[[c for c in feats if not c.startswith(("band_", "v_kurt"))] + ["rn"]].median() + 1e-3)
        s = s.join(g[[c for c in feats if c.startswith(("band_", "v_kurt"))]].median())
        s["hits_km"] = np.log1p(g.hits.sum() / (g.size() * P.WINDOW_M / 1000))
        s["emb"] = g.emb.apply(lambda e: np.median(np.stack(e), 0))
        s["paser"], s["n"] = g.paser.first(), g.size()
        return s[s.n >= 4]

    test = df.drive.str.startswith(P.TEST_PREFIX)
    tr, te = sections(df[~test]), sections(df[test])
    w = np.sqrt(tr.n)

    def ridge(Xtr, Xte):
        sc = StandardScaler().fit(Xtr)
        return RidgeCV(alphas=np.logspace(-2, 3, 20)).fit(sc.transform(Xtr), tr.paser, sample_weight=w).predict(sc.transform(Xte))

    pca = PCA(TSP_DIMS, random_state=0).fit(np.stack(tr.emb))
    tsp_tr, tsp_te = pca.transform(np.stack(tr.emb)), pca.transform(np.stack(te.emb))
    a, b = P.fit_paser(np.exp(tr.rn), tr.paser, tr.n)
    guess = round(np.average(tr.paser, weights=tr.n))
    hand = feats + ["rn", "hits_km"]
    models = [
        (f"Always guess {guess}", "baseline", np.full(len(te), float(guess))),
        ("Roughness line (M1)", "one number, hand-fitted", a + b * te.rn),
        ("Ridge on 15 signal features", "frequency bands, lateral, gyro, peaks, hit density",
         ridge(tr[hand], te[hand])),
        ("IBM Granite TSPulse alone", f"embedding of the vibration's shape, {TSP_DIMS} components", ridge(tsp_tr, tsp_te)),
        ("TSPulse + roughness", "shape plus size", ridge(np.column_stack([tsp_tr, tr.rn]), np.column_stack([tsp_te, te.rn]))),
    ]
    y = te.paser.to_numpy()
    ends = (y <= 4) | (y >= 8)
    rows = []
    for name, what, p in models:
        err = np.abs(np.clip(np.round(p), 1, 10) - y)
        varies = np.ptp(p) > 0
        rows.append({
            "name": name, "what": what,
            "within1": round(float((err <= 1).mean()), 3), "mae": round(float(err.mean()), 2),
            "spearman": round(float(spearmanr(p, y)[0]), 2) if varies else None,
            "auc_good_poor": round(float(roc_auc_score(y[ends] >= 8, p[ends])), 2) if varies else None,
        })
        print(f"{name:30s} within1 {rows[-1]['within1']:.0%}  MAE {rows[-1]['mae']}  "
              f"spearman {rows[-1]['spearman']}  AUC good vs poor {rows[-1]['auc_good_poor']}")
    (P.OUT / "models.json").write_text(json.dumps({
        "train_sections": len(tr), "test_sections": len(te),
        "test_poor": int((y <= 4).sum()), "test_good": int((y >= 8).sum()), "models": rows}, indent=1))


if __name__ == "__main__":
    main()
