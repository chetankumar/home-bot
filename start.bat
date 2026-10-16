@echo off
cd /d %~dp0
conda run --no-capture-output -n home-hub python -m uvicorn hub.main:app --host 0.0.0.0 --port 8000
