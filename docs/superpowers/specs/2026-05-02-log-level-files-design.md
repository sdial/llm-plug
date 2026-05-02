# 日志级别文件输出设计

## 目标

将 warning、error、critical 级别的日志分别写入独立文件，便于问题排查和监控。

## 方案

在 `main.py` 启动时配置 loguru 的额外 sink，按级别分流到不同文件。

## 实现细节

### 文件位置

所有日志文件存放在 `logs/` 目录：

- `logs/warning.log` — WARNING 级别
- `logs/error.log` — ERROR 级别
- `logs/critical.log` — CRITICAL 级别

### 配置代码

在 `main.py` 顶部 import 之后添加：

```python
from pathlib import Path

# 配置日志级别文件输出
_log_dir = Path(__file__).parent / "logs"
_log_dir.mkdir(exist_ok=True)

logger.add(
    _log_dir / "warning.log",
    level="WARNING",
    rotation="10 MB",
    filter=lambda r: r["level"].name == "WARNING"
)
logger.add(
    _log_dir / "error.log",
    level="ERROR",
    rotation="10 MB",
    filter=lambda r: r["level"].name == "ERROR"
)
logger.add(
    _log_dir / "critical.log",
    level="CRITICAL",
    rotation="10 MB",
    filter=lambda r: r["level"].name == "CRITICAL"
)
```

### 日志格式

使用 loguru 默认文本格式：

```
2026-05-02 14:30:00.123 | ERROR    | proxy_core:func_name - message
```

### 轮转策略

- 单文件超过 10MB 自动创建新文件
- loguru 自动处理，无需手动干预

## 影响范围

- 新增 3 个日志文件
- 现有控制台日志输出不变
- 现有 debug 日志（JSONL 格式）不变
