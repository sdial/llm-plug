"""上下文优化入口：总开关门控 + 目标格式门控 + 统计与日志。

阶段一只支持目标格式 openai-chat-completions 与 anthropic（决策三：
Responses 永不纳入上下文优化范围，永远跳过）。
"""

from __future__ import annotations

import json
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from context_optimizer.caveman import inject_caveman_prompt
from context_optimizer.compress_tool_results import compress_tool_results
from context_optimizer.strip_telemetry import strip_claude_code_telemetry
from models.api_types import APIType

# 独立分析日志级别（WARNING/ERROR 之间），由 configure_ctx_opt_logging 路由到 JSONL 文件
_CTX_OPT_LEVEL = "CTX_OPT"
_CTX_OPT_SINK_IDS: list[int] = []
# 本模块重建的 stderr 控制台 sink id（首个重建沿用 loguru 默认 handler(0)，之后自持 id）
_CTX_OPT_CONSOLE_SINK_ID: int | None = None

# 与 loguru 默认 stderr sink 一致的格式，用于重建时排除 CTX_OPT 记录
_DEFAULT_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)
# loguru 默认 stderr handler 创建时固定为 DEBUG（文档化行为）；重建控制台 handler
# 时沿用该级别，既不依赖 loguru 私有内部结构（_core.handlers），也不放宽到 TRACE。
_DEFAULT_CONSOLE_LEVEL = "DEBUG"

_SUPPORTED_TARGETS = {APIType.OPENAI_CHAT.value, APIType.ANTHROPIC.value}


def _init_logger() -> None:
    with suppress(ValueError):
        logger.level(_CTX_OPT_LEVEL, no=35)


def _target_value(target_api_type) -> str:
    if isinstance(target_api_type, APIType):
        return target_api_type.value
    return str(target_api_type)


def run_context_optimization(
    payload: dict,
    *,
    target_api_type,
    settings: dict,
    upstream_api_type=None,
) -> tuple[dict, dict]:
    """入口：门控 + 压缩 + Caveman 注入 + 统计与日志。原地修改 payload。

    target_api_type: APIType 枚举或等价字符串（客户端入口格式）。
    upstream_api_type: APIType 枚举或等价字符串（上游渠道格式）。
        未提供时默认与 target_api_type 一致。
    settings: 业务设置 dict（config.get_settings() 的返回），全部热生效。
    返回 (payload, stats)；stats 为
    {"compressed_count", "before", "after", "saved", "pct"}。
    未启用 / 非 Chat 或 Anthropic 目标 / 无实际变化时零开销短路。
    """
    stats = {"compressed_count": 0, "before": 0, "after": 0, "saved": 0, "pct": 0.0}
    if not settings.get("ctx_optimize_enabled"):
        return payload, stats
    api_value = _target_value(target_api_type)
    if api_value not in _SUPPORTED_TARGETS:
        return payload, stats
    records = compress_tool_results(payload, settings)

    # Phase 2a: 仅在 Anthropic 目标下归一化 Claude Code 遥测字段。
    telemetry_records: list[dict] = []
    if api_value == APIType.ANTHROPIC.value:
        telemetry_records = strip_claude_code_telemetry(payload, settings)

    # Phase 2b: Caveman 提示词注入（基于上游实际格式）。
    if settings.get("ctx_optimize_caveman_enabled"):
        inject_caveman_prompt(payload, upstream_api_type or target_api_type)

    all_records = records + telemetry_records
    if not all_records:
        return payload, stats
    before = sum(r["before"] for r in all_records)
    after = sum(r["after"] for r in all_records)
    saved = before - after
    pct = round(saved / before * 100, 2) if before else 0.0
    stats = {
        "compressed_count": len(all_records),
        "before": before,
        "after": after,
        "saved": saved,
        "pct": pct,
    }
    logger.debug(f"[CTX_OPT] target={api_value} before={before} after={after} saved={saved} ({pct}%)")
    _init_logger()
    for record in all_records:
        logger.log(
            _CTX_OPT_LEVEL,
            json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "target": api_value,
                    "tool_index": record.get("tool_index"),
                    "msg_index": record.get("msg_index"),
                    "location": record.get("location"),
                    "before": record["before"],
                    "after": record["after"],
                    "diff": record["before"] - record["after"],
                    "pct": round((record["before"] - record["after"]) / record["before"] * 100, 2)
                    if record["before"]
                    else 0,
                },
                ensure_ascii=False,
            ),
        )
    return payload, stats


def _exclude_ctx_opt_from_console() -> None:
    """把 stderr 控制台 sink 替换为排除 CTX_OPT 记录的版本，避免 JSON 行泄漏到控制台。

    首次调用移除 loguru 默认 handler(0)（若不存在，如纯测试环境，则跳过）；
    之后改用本模块记录的自建 console sink id 重建，保证 configure 多次调用也一致。
    重建时沿用 loguru 默认控制台级别（DEBUG），避免把过滤级别放宽到 TRACE。
    全部走 loguru 公开 API（remove/add），不依赖其私有内部结构。
    """
    global _CTX_OPT_CONSOLE_SINK_ID
    if _CTX_OPT_CONSOLE_SINK_ID is not None:
        with suppress(ValueError):
            logger.remove(_CTX_OPT_CONSOLE_SINK_ID)
    else:
        try:
            logger.remove(0)
        except ValueError:
            return
    _CTX_OPT_CONSOLE_SINK_ID = logger.add(
        sys.stderr,
        level=_DEFAULT_CONSOLE_LEVEL,
        filter=lambda r: r["level"].name != _CTX_OPT_LEVEL,
        format=_DEFAULT_CONSOLE_FORMAT,
    )


def configure_ctx_opt_logging(log_dir: str) -> None:
    """CTX_OPT 级别日志路由到 {log_dir}/context-optimization.jsonl（幂等）。"""
    global _CTX_OPT_SINK_IDS
    if _CTX_OPT_SINK_IDS:
        return
    _init_logger()
    _exclude_ctx_opt_from_console()
    path = Path(log_dir) / "context-optimization.jsonl"
    _CTX_OPT_SINK_IDS.append(
        logger.add(
            path,
            level=_CTX_OPT_LEVEL,
            filter=lambda r: r["level"].name == _CTX_OPT_LEVEL,
            format="{message}",
            rotation="10 MB",
            encoding="utf-8",
        )
    )
