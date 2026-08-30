#!/usr/bin/env bash
export AO_DATA_DIR="E:\\智理杯智能体大赛\\ao-data"
export AO_RUN_FILE="E:\\智理杯智能体大赛\\ao-data\\ao.run"
cd "E:/智理杯智能体大赛/ao-supervision-sidecar" || exit 1
python -m src.closed_loop_cli --task tasks/demo-repeated-error.json --watch
