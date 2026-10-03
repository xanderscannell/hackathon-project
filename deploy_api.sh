#!/bin/sh
# Deploy the read-only road API to IBM Code Engine (the event's project).
# No image build: the stock Python image runs backend.py, which is mounted with
# only the files the API reads (never drive tracks) from a configmap.
set -e
export MSYS_NO_PATHCONV=1  # Git Bash on Windows would rewrite /app into a Windows path
ibmcloud ce project select -n "watsonx-Hackathon Code Engine"
files="--from-file backend.py --from-file data/out/potholes.json --from-file data/out/sections.json --from-file data/out/facts.json --from-file data/out/at_risk.json"
ibmcloud ce configmap get -n road-api-files >/dev/null 2>&1 \
  && ibmcloud ce configmap update -n road-api-files $files \
  || ibmcloud ce configmap create -n road-api-files $files
# smallest size; scales to zero when idle (first request after idle takes a few seconds).
# DEPLOYED changes every run so the app restarts and reads the new files.
opts="--image python:3.13-slim --command python --argument /app/backend.py
  --env HOST=0.0.0.0 --env API_ONLY=1 --env DATA_DIR=/app --env DEPLOYED=$(date +%s)
  --port 8080 --cpu 0.125 --memory 0.25G --min-scale 0 --max-scale 1"
if ibmcloud ce app get -n road-api >/dev/null 2>&1; then
  ibmcloud ce app update -n road-api $opts
else
  ibmcloud ce app create -n road-api $opts --mount-configmap /app=road-api-files  # the mount persists across updates
fi
ibmcloud ce app get -n road-api --output url
