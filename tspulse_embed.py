"""IBM Granite TSPulse embeddings of the road signal, one per 5.12 s of driving.

    .venv-ml/Scripts/python tspulse_embed.py    # data/raw/drives -> data/out/tspulse/<drive>.npz

TSPulse rescales each window itself, so the embedding describes the shape of
the vibration, not its size; model.py tests whether that adds anything to the
roughness number. Runs in its own venv (granite-tsfm pins torch and transformers).
"""
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from tsfm_public.models.tspulse import TSPulseForReconstruction

RAW = Path("data/raw/drives")
OUT = Path("data/out/tspulse")
CTX = 512      # TSPulse context: 5.12 s at 100 Hz
STRIDE = 256   # windows overlap by half


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    model = TSPulseForReconstruction.from_pretrained(
        "ibm-granite/granite-timeseries-tspulse-r1", revision="tspulse-hybrid-dualhead-512-p8-r1",
        num_input_channels=1, mask_type="user").eval()
    for f in sorted(RAW.glob("*_imu.csv")):
        stem = f.name.removesuffix("_imu.csv")
        imu = pd.read_csv(f)
        v = (imu.vert - imu.vert.rolling(100, center=True, min_periods=1).mean()).to_numpy(np.float32)
        starts = np.arange(0, len(v) - CTX + 1, STRIDE)
        x = torch.from_numpy(np.stack([v[s:s + CTX] for s in starts]))[..., None]
        embs = []
        with torch.no_grad():
            for b in range(0, len(x), 256):
                xb = x[b:b + 256]
                out = model(past_values=xb, past_observed_mask=torch.ones_like(xb))
                embs.append(out["backbone_hidden_state"][:, 0, :].numpy())
        t_mid = imu.t_ms.to_numpy(float)[starts + CTX // 2] / 1000
        np.savez_compressed(OUT / f"{stem}.npz", t=t_mid, emb=np.concatenate(embs).astype(np.float16))
        print(stem, len(starts), "windows")


if __name__ == "__main__":
    main()
