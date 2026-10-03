# Road Condition Map

Fleet vehicles measure road roughness as they drive; the map shows it against the PASER ratings Michigan already keeps.

## Run

```
python pipeline.py            # data/raw/drives -> data/out/*.json
python pipeline.py --selftest
python backend.py              # dashboard + read-only API, http://localhost:8000
python backend.py --selftest

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

## Road Desk agent (watsonx Orchestrate)

```
.venv/Scripts/orchestrate connections add -a road_api
.venv/Scripts/orchestrate connections configure -a road_api --env draft -k key_value -t team   # and --env live
.venv/Scripts/orchestrate tools import -k python -f orchestrate/road_tools.py -a road_api
.venv/Scripts/orchestrate agents import -f orchestrate/road_desk.yaml
.venv/Scripts/orchestrate agents deploy -n road_desk
.venv/Scripts/python orchestrate/webchat_config.py      # embeds the chat in the dashboard

cloudflared tunnel --url http://localhost:8000          # public URL for the agent's tools
.venv/Scripts/orchestrate connections set-credentials -a road_api --env draft -e "url=<tunnel url>"   # and --env live
```

The tunnel makes the backend public. Through it only `/api/` answers; the dashboard and `data/out/` (raw drive tracks) stay local.

## Deterioration forecast

```
.venv-ml/Scripts/python ttm_forecast.py    # IBM Granite TTM, zero-shot and fine-tuned -> data/out/ttm_2020.npz
python forecast.py                         # backtest 2020 -> 2024 and 2028 risk per segment -> data/out/forecast.json
```

Backtested on SEMCOG's own PASER history: models see nothing after 2020 and are scored on the real 2024 ratings.
