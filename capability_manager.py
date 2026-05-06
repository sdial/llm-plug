"""
Provider Capability 管理模块

根据渠道配置推断上游提供商的能力，并在请求转发前过滤不支持的参数。
"""
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class ProviderCapabilities:
    """提供商能力配置"""
    supports_parallel_tool_calls: bool = True
    supports_tool_choice_auto: bool = True
    requires_single_system_message: bool = False
    filter_think_content: bool = False


def infer_capabilities(channel) -> ProviderCapabilities:
    """
    根据渠道配置推断提供商能力

    通过 base_url 中的关键词识别提供商，返回对应的能力配置。
    用户可以在 channels.json 中通过 capabilities 字段覆盖默认值。

    Args:
        channel: Channel 模型实例

    Returns:
        ProviderCapabilities 实例
    """
    base_url = (channel.base_url or "").lower()

    # 优先使用用户配置的能力
    if hasattr(channel, 'capabilities') and channel.capabilities:
        caps_dict = channel.capabilities if isinstance(channel.capabilities, dict) else {}
        return ProviderCapabilities(
            supports_parallel_tool_calls=caps_dict.get('supports_parallel_tool_calls', True),
            supports_tool_choice_auto=caps_dict.get('supports_tool_choice_auto', True),
            requires_single_system_message=caps_dict.get('requires_single_system_message', False),
            filter_think_content=caps_dict.get('filter_think_content', False),
        )

    # DeepSeek: 不支持并行工具调用，需要过滤 💭
    if "deepseek" in base_url:
        return ProviderCapabilities(
            supports_parallel_tool_calls=False,
            filter_think_content=True,
        )

    # MiniMax: 要求单条前置 system 消息
    if "minimax" in base_url:
        return ProviderCapabilities(
            requires_single_system_message=True,
        )

    # 默认：全部支持
    return ProviderCapabilities()


def apply_capability_filter(request_data: dict, caps: ProviderCapabilities) -> dict:
    """
    根据能力过滤请求参数

    当请求参数超出渠道能力时，自动移除或降级处理。
    会记录警告日志，但不阻断请求。

    Args:
        request_data: 原始请求数据
        caps: 提供商能力配置

    Returns:
        过滤后的请求数据
    """
    result = dict(request_data)

    # 过滤 parallel_tool_calls
    if not caps.supports_parallel_tool_calls:
        if "parallel_tool_calls" in result:
            del result["parallel_tool_calls"]
            logger.warning(f"[CAPABILITY] 降级: parallel_tool_calls 被移除（渠道不支持）")

    # 过滤 tool_choice
    if not caps.supports_tool_choice_auto:
        tc = result.get("tool_choice")
        if tc == "auto":
            result["tool_choice"] = "none"
            logger.warning(f"[CAPABILITY] 降级: tool_choice auto → none（渠道不支持）")

    return result


def merge_system_messages(messages: list[dict]) -> list[dict]:
    """
    合并多条 system 消息为单条

    MiniMax 等提供商要求 system 消息只能有一条，且必须在最前面。
    此函数将所有 system 消息合并为一条，保留其他消息顺序。

    Args:
        messages: 原始消息列表

    Returns:
        合并后的消息列表
    """
    system_parts = []
    other_messages = []

    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if content:
                system_parts.append(content)
        else:
            other_messages.append(msg)

    if system_parts:
        merged = {"role": "system", "content": "\n\n".join(system_parts)}
        return [merged] + other_messages
    return other_messages
