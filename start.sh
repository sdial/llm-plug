#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 默认模式: run (正常运行) / debug (调试运行)
MODE="${1:-run}"

# 环境变量（按需修改或 export 覆盖）
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-55555}"
export WORKERS="${WORKERS:-2}"
export LOG_LEVEL="${LOG_LEVEL:-info}"

# 安装依赖（若尚未安装）
if [ ! -d ".venv" ]; then
    echo ">>> 首次运行，安装依赖..."
    uv sync
fi

case "$MODE" in
    run)
        echo ">>> 正常运行 -> http://${HOST}:${PORT} (workers=${WORKERS})"
        uv run uvicorn main:app \
            --host "$HOST" \
            --port "$PORT" \
            --workers "$WORKERS" \
            --loop uvloop \
            --http httptools \
            --timeout-keep-alive 360 \
            --log-level "$LOG_LEVEL" \
            --access-log \
            --no-use-colors \
            --no-server-header
        ;;
    debug)
        echo ">>> 调试运行 (reload + trace + debug日志) -> http://${HOST}:${PORT}"
        export LOG_LEVEL=debug
        export DEBUG=true
        uv run uvicorn main:app \
            --host "$HOST" \
            --port "$PORT" \
            --loop uvloop \
            --http httptools \
            --timeout-keep-alive 360 \
            --reload \
            --log-level trace \
            --access-log \
            --use-colors
        ;;
    *)
        echo "用法: $0 [run|debug]"
        echo "  run   - 正常运行（默认）"
        echo "  debug - 调试运行（热重载）"
        exit 1
        ;;
esac
