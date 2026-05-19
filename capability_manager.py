"""
Provider Capability 管理模块

根据渠道配置推断上游提供商的能力，并在请求转发前过滤不支持的参数。
"""
from dataclasses import dataclass

from loguru import logger


@dataclass
class ProviderCapabilities:
    """提供商能力配置"""
    supports_parallel_tool_calls: bool = True
    supports_tool_choice_auto: bool = True
    supports_response_format: bool = True
    supports_reasoning_effort: bool = True
    supports_file_content: bool = False
    supports_audio_content: bool = False
    supports_tool_choice_required: bool = True
    supports_strict_tools: bool = True
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
            supports_response_format=caps_dict.get('supports_response_format', True),
            supports_reasoning_effort=caps_dict.get('supports_reasoning_effort', True),
            supports_file_content=caps_dict.get('supports_file_content', False),
            supports_audio_content=caps_dict.get('supports_audio_content', False),
            supports_tool_choice_required=caps_dict.get('supports_tool_choice_required', True),
            supports_strict_tools=caps_dict.get('supports_strict_tools', True),
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
            logger.warning("[CAPABILITY] 降级: parallel_tool_calls 被移除（渠道不支持）")

    # 过滤 tool_choice=auto
    # 注意：不能把 auto 改成 none —— 这是语义反转（auto=允许调用工具，none=禁止）。
    # 正确降级是删除字段，让上游使用默认行为（OpenAI/Anthropic 默认即 auto-like）。
    if not caps.supports_tool_choice_auto:
        tc = result.get("tool_choice")
        if tc == "auto":
            del result["tool_choice"]
            logger.warning("[CAPABILITY] 降级: tool_choice auto 被移除，回退上游默认（渠道不支持显式 auto）")

    # 过滤 tool_choice=required
    if not caps.supports_tool_choice_required:
        tc = result.get("tool_choice")
        if tc == "required":
            del result["tool_choice"]
            logger.warning("[CAPABILITY] 降级: tool_choice required 被移除（渠道不支持）")

    # 过滤 response_format
    if not caps.supports_response_format:
        if "response_format" in result:
            del result["response_format"]
            logger.warning("[CAPABILITY] 降级: response_format 被移除（渠道不支持）")

    # 过滤 reasoning_effort
    if not caps.supports_reasoning_effort:
        if "reasoning_effort" in result:
            del result["reasoning_effort"]
            logger.warning("[CAPABILITY] 降级: reasoning_effort 被移除（渠道不支持）")

    # 过滤 strict tools
    if not caps.supports_strict_tools:
        tools = result.get("tools")
        if isinstance(tools, list):
            for tool in tools:
                if isinstance(tool, dict) and "function" in tool:
                    func = tool["function"]
                    if isinstance(func, dict) and "strict" in func:
                        del func["strict"]
                        logger.warning("[CAPABILITY] 降级: function strict 被移除（渠道不支持）")

    # 过滤 file content in messages
    if not caps.supports_file_content:
        messages = result.get("messages")
        if isinstance(messages, list):
            result["messages"] = _filter_content_type(messages, "file")

    # 过滤 audio content in messages
    if not caps.supports_audio_content:
        messages = result.get("messages")
        if isinstance(messages, list):
            result["messages"] = _filter_content_type(messages, "input_audio")

    return result


def _filter_content_type(messages: list, content_type: str) -> list:
    """从消息列表中移除指定类型的内容块，返回新列表，不修改入参。"""
    new_messages: list = []
    warned = False
    for msg in messages:
        if not isinstance(msg, dict):
            new_messages.append(msg)
            continue
        content = msg.get("content")
        if isinstance(content, list):
            filtered = [p for p in content if not (isinstance(p, dict) and p.get("type") == content_type)]
            if len(filtered) < len(content):
                msg_copy = dict(msg)
                msg_copy["content"] = filtered
                new_messages.append(msg_copy)
                if not warned:
                    logger.warning(f"[CAPABILITY] 降级: {content_type} 内容块被移除（渠道不支持）")
                    warned = True
                continue
        new_messages.append(msg)
    return new_messages


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
    system_parts: list[str] = []
    other_messages = []

    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str):
                if content:
                    system_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        if isinstance(text, str) and text:
                            system_parts.append(text)
                    elif isinstance(part, str) and part:
                        system_parts.append(part)
        else:
            other_messages.append(msg)

    if system_parts:
        merged = {"role": "system", "content": "\n\n".join(system_parts)}
        return [merged] + other_messages
    return other_messages
