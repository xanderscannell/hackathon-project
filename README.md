# Road Condition Map

Fleet vehicles measure road roughness as they drive; the map shows it against the PASER ratings Michigan already keeps.

## Run

```
python pipeline.py            # data/raw/drives -> data/out/*.json
python pipeline.py --selftest
python -m http.server 8000    # then open http://localhost:8000
```

`data/` is gitignored. Expected layout: `data/raw/drives/<drive>_{imu,gps,markers}.csv` and `data/raw/semcog_paser_commute.geojson`.
