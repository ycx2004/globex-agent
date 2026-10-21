#!/usr/bin/env bash
# 本地一键启动：后端 FastAPI(8000) + 前端 Vite(默认 5173)
# 外部传入的 --host/--port 参数会透传给 Vite
set -e
cd "$(dirname "$0")/.."

uv run python -m uvicorn app.presentation.server:app --host 127.0.0.1 --port 8000 --workers 1 &
BACKEND_PID=$!
trap 'kill $BACKEND_PID 2>/dev/null' EXIT INT TERM

npm --prefix frontend run dev -- "$@"
