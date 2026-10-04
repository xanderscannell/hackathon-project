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

## Device

ESP32 + BNO085 + GT-U7 GPS + microSD. Wiring (`device/src/main.cpp`): BNO085 on I2C (SDA 21, SCL 22), GPS TX to GPIO 16 and RX to 17, SD on SPI (SCK 18, MISO 19, MOSI 23, CS 5). It finds severe hits and 50 m roughness windows on the board (`device/src/detect.h`, the same math as `pipeline.py`), queues them on the SD card, and uploads the queue over WiFi. Raw IMU and GPS stay on the card in `/drives/`, in the same CSV format as `data/raw/drives`.

```
cd device && pio run -t upload                 # flash; a retry fixes "serial noise" on board 1
HOST=0.0.0.0 python backend.py                 # accepts POST /live from the local network
```

The first boot writes `/config.txt` on the card. Set `ssid`, `pass`, `url=http://<laptop ip>:8000/live`, and `replay=/replay/<drive>` there, or send the same `key=value` lines over serial (115200). To replay a drive, copy `<drive>_imu.csv` and `<drive>_gps.csv` from `data/raw/drives` to `/replay/` on the card, then press BOOT (or send `replay`; `speed=8` is 8x real time). The dashboard's Device view draws what the board uploads. On a replay of a logged drive, it also rings the backend's hits for that drive. Another machine on the network can only post to `/live` and read `/api/`.

```
python device/check.py     # the device's detector, built for the laptop, against pipeline.py on every drive
```

`check.py` needs a laptop g++ once: `pio pkg install -g -t platformio/toolchain-gccmingw32`.

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

sh deploy_api.sh                                        # read-only API on IBM Code Engine, prints its URL
.venv/Scripts/orchestrate connections set-credentials -a road_api --env draft -e "url=<api url>"   # and --env live
```

The deployed API is public, so it answers only `/api/` and carries only the data files the API reads; the dashboard and the raw drive tracks stay local. For a quick local test, `cloudflared tunnel --url http://localhost:8000` works too: requests through it also get `/api/` only.

## Deterioration forecast

```
.venv-ml/Scripts/python ttm_forecast.py    # IBM Granite TTM, zero-shot and fine-tuned -> data/out/ttm_2020.npz
python forecast.py                         # backtest 2020 -> 2024 and 2028 risk per segment -> data/out/forecast.json
```

Backtested on SEMCOG's own PASER history: models see nothing after 2020 and are scored on the real 2024 ratings.
