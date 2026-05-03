#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 默认模式: run (正常运行) / debug (调试运行)
MODE="${1:-run}"

# 环境变量（按需修改或 export 覆盖）
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-55555}"
export WORKERS="${WORKERS:-1}"
export LOG_LEVEL="${LOG_LEVEL:-info}"

# 清理函数：杀死所有子进程
cleanup() {
    echo ""
    echo ">>> 正在停止服务..."
    # 杀死当前进程组的所有进程
    if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" ]]; then
        # Windows: 使用 taskkill 杀死进程树
        local main_pid=$$
        taskkill //F //T //PID $main_pid 2>/dev/null || true
    else
        # Unix: 杀死进程组
        kill -TERM -- -$$ 2>/dev/null || true
    fi
    # 额外清理：杀死残留的端口监听进程
    sleep 1
    if command -v lsof &> /dev/null; then
        lsof -ti:$PORT | xargs -r kill -9 2>/dev/null || true
    fi
    echo ">>> 服务已停止"
    exit 0
}

# 捕获 SIGINT (CTRL+C) 和 SIGTERM
trap cleanup SIGINT SIGTERM

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
    --http httptools \
    --loop auto \
    --timeout-keep-alive 360 \
    --log-level "$LOG_LEVEL" \
    --access-log \
    --no-use-colors \
    --no-server-header \
    --ws none \
    --backlog 2048
    ;;
debug)
    echo ">>> 调试运行 (reload + trace + debug日志) -> http://${HOST}:${PORT}"
    export LOG_LEVEL=debug
    export DEBUG=true
    uv run uvicorn main:app \
    --host "$HOST" \
    --port "$PORT" \
    --http httptools \
    --loop auto \
    --timeout-keep-alive 360 \
    --reload \
    --log-level trace \
    --access-log \
    --use-colors
    ;;
*)
    echo "用法: $0 [run|debug]"
    echo " run - 正常运行（默认）"
    echo " debug - 调试运行（热重载）"
    exit 1
    ;;
esac
