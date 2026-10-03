# Road Condition Map

Fleet vehicles measure road roughness as they drive; the map shows it against the PASER ratings Michigan already keeps.

## Run

```
python pipeline.py            # data/raw/drives -> data/out/*.json
python pipeline.py --selftest
python -m http.server 8000    # then open http://localhost:8000

# weekly brief (needs the Orchestrate ADK in .venv and an active env)
.venv/Scripts/orchestrate tools import -k flow -f orchestrate/weekly_brief.py
.venv/Scripts/python brief.py              # data/out/facts.json -> Granite -> data/out/brief.json
```

`data/` is gitignored. Expected layout: `data/raw/drives/<drive>_{imu,gps,markers}.csv` and `data/raw/semcog_paser_commute.geojson`.

## Model comparison

```
python -m venv .venv-ml && .venv-ml/Scripts/python -m pip install granite-tsfm "transformers[torch]<5"
.venv-ml/Scripts/python tspulse_embed.py   # IBM Granite TSPulse embeddings -> data/out/tspulse/
python model.py                            # every model on the same held-out sections -> data/out/models.json
```

`transformers` 5.x breaks TSPulse's output shapes, hence the pin.
