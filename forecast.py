"""Which rated roads will fall to poor? Backtested on SEMCOG's PASER history, then run forward.

    .venv-ml/Scripts/python ttm_forecast.py   # optional: adds IBM Granite TTM rows to the backtest
    python forecast.py                        # -> data/out/forecast.json

Backtest: models learn from forecasts made in 2012, 2014 and 2016 (four years
ahead, so nothing after 2020), then forecast 2024 from history through 2020
and are scored against the real 2024 ratings, weighted by miles. Forward: the
same classifier, also taught 2018 and 2020, gives each fair or good road its
chance of being poor (PASER 1-4) by 2028.
"""
import json

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.metrics import roc_auc_score

import pipeline as P

YEARS = list(range(2003, 2025))
HORIZON = 4
POOR = 4          # Michigan: 1-4 poor
TOP_SHARE = 0.10  # share of fair/good miles flagged as most at risk


def load():
    props = pd.DataFrame([f["properties"] for f in json.loads((P.RAW / "semcog_paser_commute.geojson").read_text())["features"]])
    h = props[[f"AS_OF_{y % 100:02d}" for y in YEARS]].to_numpy(float)
    h[h == 0] = np.nan  # 0 = not rated that year
    return props, h


def features(props, h, T):
    """What is known at the end of year T."""
    i = YEARS.index(T)
    past = h[:, :i + 1]
    jumps = np.diff(past, axis=1) >= 2  # a rise of 2+ grades: resurfaced
    last = np.where(jumps.any(1), jumps.shape[1] - 1 - np.argmax(jumps[:, ::-1], 1), -1)
    return pd.DataFrame({
        "rating": past[:, -1], "rating_1y": past[:, -2], "rating_2y": past[:, -3], "rating_4y": past[:, -5],
        "max_6y": pd.DataFrame(past[:, -6:]).max(axis=1), "min_6y": pd.DataFrame(past[:, -6:]).min(axis=1),
        "change_6y": past[:, -1] - past[:, -7],
        "years_since_resurfaced": np.where(last >= 0, jumps.shape[1] - last, i + 1),
        "road_class": props.NFC, "legal_system": props.LEGALSYSTE, "lanes": props.LANES,
        "concrete": (props.SURFACE == "Concrete").astype(int),
    })


def examples(props, h, origins, miles):
    rows = []
    for T in origins:
        f = features(props, h, T).assign(target=h[:, YEARS.index(T + HORIZON)], miles=miles, seg=np.arange(len(h)))
        rows.append(f[f.rating.notna() & f.target.notna()])
    return pd.concat(rows, ignore_index=True)


def main():
    props, h = load()
    miles = props.LENMI.to_numpy()
    train = examples(props, h, (2012, 2014, 2016), miles)
    test = examples(props, h, (2020,), miles)
    X = [c for c in train.columns if c not in ("target", "miles", "seg")]
    y, w = test.target.to_numpy(), test.miles.to_numpy()
    at_risk = (test.rating > POOR).to_numpy()   # fair or good in 2020
    fell = y[at_risk] <= POOR

    regressor = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=15, random_state=0)
    regressor.fit(train[X], train.target, sample_weight=train.miles)
    by_rating = (train.assign(change=train.miles * (train.target - train.rating)).groupby("rating").change.sum()
                 / train.groupby("rating").miles.sum())
    forecasts = [
        ("Nothing changes", "2024 = 2020 rating", test.rating.to_numpy()),
        ("Average change by rating", "typical 4-year change for the 2020 rating", (test.rating + test.rating.map(by_rating)).to_numpy()),
        ("Gradient boosting", "rating history, road class, surface, lanes", regressor.predict(test[X])),
    ]
    ttm = P.OUT / "ttm_2020.npz"
    if ttm.exists():
        z = np.load(ttm)
        forecasts += [("IBM Granite TTM, zero-shot", "rating history only, no training", z["zero_shot"][test.seg]),
                      ("IBM Granite TTM, fine-tuned", "rating history only, tuned on 2006-2016", z["fine_tuned"][test.seg])]

    rows = []
    for name, what, p in forecasts:
        err = np.abs(np.clip(np.round(p), 1, 10) - y)
        # nothing-changes ties every road at the same rating; ranking by it is ranking by the rating
        auc = roc_auc_score(fell, -p[at_risk], sample_weight=w[at_risk])
        rows.append({"name": name, "what": what, "mae": round(float(np.average(err, weights=w)), 2),
                     "within1": round(float(np.average(err <= 1, weights=w)), 3), "auc_fall": round(float(auc), 2)})
        print(f"{name:30s} MAE {rows[-1]['mae']}  within1 {rows[-1]['within1']:.0%}  falls-to-poor AUC {rows[-1]['auc_fall']}")

    # the question an agency asks: of the fair/good roads, which become poor?
    def classifier(data):
        d = data[data.rating > POOR]
        return HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=15, random_state=0).fit(
            d[X], d.target <= POOR, sample_weight=d.miles)
    risk = classifier(train).predict_proba(test[X][at_risk])[:, 1]
    order = np.argsort(-risk)
    flagged = order[np.cumsum(w[at_risk][order]) <= TOP_SHARE * w[at_risk].sum()]
    hit = float(np.average(fell[flagged], weights=w[at_risk][flagged]))
    base = float(np.average(fell, weights=w[at_risk]))
    print(f"risk classifier AUC {roc_auc_score(fell, risk, sample_weight=w[at_risk]):.2f}; of the top "
          f"{TOP_SHARE:.0%} of fair/good miles flagged in 2020, {hit:.0%} were poor by 2024 (all fair/good: {base:.0%})")

    # forward: learn from every forecast that can be checked, then look ahead from 2024
    now = features(props, h, 2024)
    fair_good = (now.rating > POOR).to_numpy()
    forward = classifier(examples(props, h, (2012, 2014, 2016, 2018, 2020), miles)).predict_proba(now[X][fair_good])[:, 1]
    (P.OUT / "forecast.json").write_text(json.dumps({
        "backtest": {"origin": 2020, "target": 2020 + HORIZON, "miles": round(float(w.sum())), "models": rows,
                     "risk_auc": round(float(roc_auc_score(fell, risk, sample_weight=w[at_risk])), 2),
                     "top_share": TOP_SHARE, "top_hit": round(hit, 3), "base_rate": round(base, 3)},
        "forward": {"from": 2024, "to": 2024 + HORIZON,
                    "risk": [[int(s), round(float(r), 3)] for s, r in zip(np.flatnonzero(fair_good), forward)]},
    }, separators=(",", ":")))
    print(f"wrote forecast.json: {fair_good.sum()} fair/good segments with a 2028 risk")


if __name__ == "__main__":
    main()
