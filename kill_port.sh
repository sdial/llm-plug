#!/usr/bin/env bash
# 清理残留进程脚本
# 用法: ./kill_port.sh [端口号]

PORT="${1:-55555}"

echo ">>> 查找监听端口 $PORT 的进程..."

if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" ]]; then
    # Windows (Git Bash)
    echo ">>> Windows 环境，使用 netstat + taskkill"
    PIDS=$(netstat -ano 2>/dev/null | grep ":$PORT " | awk '{print $5}' | sort -u)
    if [[ -n "$PIDS" ]]; then
        echo ">>> 找到以下 PID: $PIDS"
        for pid in $PIDS; do
            if [[ "$pid" =~ ^[0-9]+$ ]]; then
                echo ">>> 终止进程 PID: $pid"
                taskkill //F //PID "$pid" 2>/dev/null || echo "无法终止 PID: $pid"
            fi
        done
    else
        echo ">>> 未找到监听端口 $PORT 的进程"
    fi
else
    # Linux/Mac/WSL
    if command -v lsof &> /dev/null; then
        PIDS=$(lsof -ti:$PORT 2>/dev/null || true)
        if [[ -n "$PIDS" ]]; then
            echo ">>> 找到以下 PID: $PIDS"
            echo "$PIDS" | xargs -r kill -9 2>/dev/null
            echo ">>> 已终止"
        else
            echo ">>> 未找到监听端口 $PORT 的进程"
        fi
    else
        echo ">>> lsof 未安装，请手动查找进程"
    fi
fi
