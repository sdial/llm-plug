from abc import ABC, abstractmethod
from typing import Any
import json
import logging

logger = logging.getLogger(__name__)


def safe_parse_tool_args(value: Any) -> tuple[Any, bool]:
    """安全解析 tool arguments JSON 字符串。

    Args:
        value: 待解析的值，通常是 JSON 字符串或已解析的对象

    Returns:
        tuple: (解析后的值, 是否为完整解析)
            - 如果解析成功，返回 (解析后的对象, True)
            - 如果解析失败，返回 {"_partial_args": 原始字符串}, False)
            - 如果输入不是字符串，直接返回 (原值, True)
    """
    if not isinstance(value, str):
        return value, True

    try:
        return json.loads(value), True
    except json.JSONDecodeError:
        logger.warning(
            "incomplete tool arguments JSON: %r",
            value[:120] if len(value) > 120 else value,
        )
        return {"_partial_args": value}, False


# Anthropic thinking.budget_tokens -> OpenAI reasoning_effort 的统一阈值。
# 对应 to_anthropic.py 的反向映射 (low=1024, medium=4096, high=16384)：
# - <=2048 落在 low 区间（1024 的邻域）
# - <=8192 落在 medium 区间（4096 的邻域）
# - >8192  落在 high 区间（>=16384 的邻域）
THINKING_BUDGET_LOW_MAX = 2048
THINKING_BUDGET_MEDIUM_MAX = 8192


def thinking_budget_to_effort(budget: int | None) -> str:
    """将 Anthropic thinking.budget_tokens 映射为 OpenAI reasoning_effort。"""
    if not budget or budget <= 0:
        return "low"
    if budget <= THINKING_BUDGET_LOW_MAX:
        return "low"
    if budget <= THINKING_BUDGET_MEDIUM_MAX:
        return "medium"
    return "high"


class BaseConverter(ABC):
    """转换器基类，定义格式转换接口。

    `source_type` 由 proxy_core 传入，为上游渠道 `Channel.api_type` 的字符串值
    （如 ``openai-chat-completions``），多源转换实现可按需分支。
    """

    @abstractmethod
    def convert_request(
        self, source_data: dict[str, Any], source_type: str = ""
    ) -> dict[str, Any]:
        """将入口请求体转为上游 API 所需 JSON。"""
        pass

    @abstractmethod
    def convert_response(
        self, target_response: dict[str, Any], source_type: str = ""
    ) -> dict[str, Any]:
        """将上游非流式 JSON 转为入口 API 对应格式。"""
        pass

    @abstractmethod
    def convert_stream_chunk(
        self, chunk: dict[str, Any], source_type: str = ""
    ) -> dict[str, Any] | None:
        """将上游 SSE 解析出的单条 JSON 转为入口格式；返回 None 表示跳过该块。"""
        pass

    def get_stream_event_type(
        self, chunk: dict[str, Any], source_type: str = ""
    ) -> str | None:
        """获取流式事件的 event type（仅 Anthropic 输出格式需要）。

        默认实现从 chunk 的 _event_type 字段读取；
        子类可在 convert_stream_chunk 中缓存 event type 后覆盖此方法。
        """
        if isinstance(chunk, dict) and chunk.get("_event_type"):
            return chunk["_event_type"]
        return None

    def get_extra_events(self, chunk: dict[str, Any]) -> list:
        """获取流式转换产生的额外事件。子类可覆盖。"""
        if isinstance(chunk, dict) and chunk.get("_extra_events"):
            return chunk["_extra_events"]
        return []

    def finalize_stream(self, source_type: str = "") -> list[dict[str, Any]]:
        """在上游流结束时补发必要的收尾事件；默认无需额外事件。"""
        return []
