import json
import logging
from abc import ABC, abstractmethod
from typing import Any

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

    `source_type` 由 proxy 传入，为选定接入点 `Endpoint.api_type` 的字符串值
    （如 ``openai-chat-completions``），多源转换实现按类级映射表
    （source_type → handler，见各子类的 ``_*_HANDLERS``）分发。

    **流式转换协议（两拍，ADR-0016 D0）**

    - 拍 1 —— 每个上游 chunk 调一次 ``convert_stream_chunk(chunk, source_type)``，
      返回该拍产生的**全部**事件（``list[dict]``，空列表 = 本拍无产出）。
    - 拍 2 —— 上游流结束时调一次 ``finalize_stream(source_type)``，返回收尾事件
      （``list[dict]``，可为空列表）。

    此外无第三拍："下一拍排空 stash" 不是协议的一部分（原 ``get_extra_events``
    已废除，额外事件并入拍 1 的返回列表）。事件统一 dict 形态：Anthropic /
    Responses 目标事件的协议类型内嵌于事件 dict 的 ``type`` 字段，SSE ``event:``
    行由 ``format_sse_for_list`` 从 ``type`` 推断；Chat 目标事件为
    chat.completion.chunk dict（仅 ``data:`` 行）。
    """

    @abstractmethod
    def convert_request(self, source_data: dict[str, Any], source_type: str = "") -> dict[str, Any]:
        """将入口请求体转为上游 API 所需 JSON。"""
        pass

    @abstractmethod
    def convert_response(self, target_response: dict[str, Any], source_type: str = "") -> dict[str, Any]:
        """将上游非流式 JSON 转为入口 API 对应格式。"""
        pass

    @abstractmethod
    def convert_stream_chunk(self, chunk: dict[str, Any], source_type: str = "") -> list[dict[str, Any]]:
        """流式拍 1：将上游 SSE 解析出的单条 JSON 转为该拍全部事件（空列表 = 本拍无产出）。"""
        pass

    def _dispatch_source(self, handlers: dict[str, Any], source_type: str, *args: Any) -> Any:
        """类级映射表分发（source_type → handler），错误文案全场统一。"""
        handler = handlers.get(source_type)
        if handler is None:
            raise ValueError(f"{type(self).__name__} 不支持 source_type={source_type!r}")
        return handler(self, *args)

    def finalize_stream(self, source_type: str = "") -> list[dict[str, Any]]:
        """流式拍 2：在上游流结束时补发必要的收尾事件；默认无需额外事件。"""
        return []
