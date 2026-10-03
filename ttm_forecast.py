"""IBM Granite TTM forecasts of each segment's PASER rating, for forecast.py's backtest.

    .venv-ml/Scripts/python ttm_forecast.py    # -> data/out/ttm_2020.npz

Forecasts the 2024 rating from history through 2020, two ways: zero-shot, and
fine-tuned on Michigan's own rating histories (forecasts made from 2006-2015,
validated on 2016, never seeing a rating after 2020).
"""
import json
from pathlib import Path

import numpy as np
import torch
from tsfm_public.toolkit.get_model import get_model

RAW = Path("data/raw/semcog_paser_commute.geojson")
OUT = Path("data/out/ttm_2020.npz")
YEARS = list(range(2003, 2025))
CTX, PRED = 52, 16      # the shortest-context TTM; yearly series are left-padded and masked
ORIGIN, HORIZON = 2020, 4
LAST_SEEN = 2020        # fine-tuning never sees a rating after this year


def history():
    feats = json.loads(RAW.read_text())["features"]
    h = np.array([[f["properties"][f"AS_OF_{y % 100:02d}"] for y in YEARS] for f in feats], float)
    h[h == 0] = np.nan  # 0 = not rated that year
    return h


def window(H, T):
    """Context ending at T (gaps carried forward, leading gaps masked) and the following PRED years."""
    i = YEARS.index(T)
    h = H[:, :i + 1].copy()
    obs = ~np.isnan(h)
    for k in range(1, h.shape[1]):
        h[:, k] = np.where(np.isnan(h[:, k]), h[:, k - 1], h[:, k])
    first = np.array([r[~np.isnan(r)][0] if (~np.isnan(r)).any() else 5.0 for r in h])
    h = np.where(np.isnan(h), first[:, None], h)
    x = np.concatenate([np.repeat(h[:, :1], CTX - h.shape[1], 1), h], 1)
    m = np.concatenate([np.zeros((len(h), CTX - h.shape[1])), obs], 1)
    fut = np.full((len(h), PRED), np.nan)
    for k in range(PRED):
        if T + 1 + k <= LAST_SEEN:
            fut[:, k] = H[:, YEARS.index(T + 1 + k)]
    return x, m, fut, obs[:, -1]


def predict(model, x, m):
    with torch.no_grad():
        out = model(past_values=torch.tensor(x, dtype=torch.float32)[..., None],
                    past_observed_mask=torch.tensor(m, dtype=torch.bool)[..., None],
                    freq_token=torch.zeros(len(x), dtype=torch.long))  # 0 = out of vocabulary: no yearly token
    return out.prediction_outputs[:, HORIZON - 1, 0].numpy()


def fine_tune(model, H):
    torch.manual_seed(0)
    rng = np.random.default_rng(0)
    def stack(origins):
        parts = [window(H, T) for T in origins]
        x, m, f, ok = (np.concatenate(p) for p in zip(*parts))
        return x[ok], m[ok], f[ok]
    train, val = stack(range(2006, 2016)), stack([2016])
    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)

    def epoch(x, m, f, learn):
        model.train(learn)
        idx = rng.permutation(len(x)) if learn else np.arange(len(x))
        total = 0.0
        for b in range(0, len(x), 512):
            j = idx[b:b + 512]
            with torch.set_grad_enabled(learn):
                out = model(past_values=torch.tensor(x[j], dtype=torch.float32)[..., None],
                            past_observed_mask=torch.tensor(m[j], dtype=torch.bool)[..., None],
                            freq_token=torch.zeros(len(j), dtype=torch.long)).prediction_outputs[..., 0]
                target = torch.tensor(f[j], dtype=torch.float32)
                seen = ~torch.isnan(target)  # loss only on years that were rated and not after LAST_SEEN
                loss = ((out - torch.nan_to_num(target)) ** 2)[seen].mean()
                if learn:
                    opt.zero_grad()
                    loss.backward()
                    opt.step()
            total += loss.item() * len(j)
        return total / len(x)

    best, state = np.inf, None
    for ep in range(8):
        tr, va = epoch(*train, True), epoch(*val, False)
        print(f"epoch {ep}: train {tr:.3f}, validation {va:.3f}")
        if va < best:
            best, state = va, {k: v.clone() for k, v in model.state_dict().items()}
        elif ep >= 2:
            break
    model.load_state_dict(state)
    return model.eval()


def main():
    H = history()
    load = lambda: get_model("ibm-granite/granite-timeseries-ttm-r2", context_length=CTX,
                             prediction_length=PRED, prefer_longer_context=False).eval()
    x, m, _, ok = window(H, ORIGIN)
    out = {}
    for name, model in [("zero_shot", load()), ("fine_tuned", fine_tune(load(), H))]:
        p = np.full(len(H), np.nan)
        p[ok] = predict(model, x[ok], m[ok])
        out[name] = p
    np.savez(OUT, **out)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
