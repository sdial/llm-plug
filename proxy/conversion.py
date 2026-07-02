from typing import Any

from config import get_setting
from converters.to_anthropic import ToAnthropicConverter
from converters.to_chat import ToChatCompletionsConverter
from converters.to_response import ToResponseConverter
from models.api_types import APIType
from models.channel import Channel
from response_state import get_responses_store

_responses_store = get_responses_store()


CONVERTER_MAP: dict[tuple[str, str], tuple[type, type]] = {
    # key: (source=上游渠道格式, target=客户端入口格式)
    # value: (RequestConverter, ResponseConverter)
    # RequestConverter: 把客户端格式(target)转换为上游格式(source)
    # ResponseConverter: 把上游格式(source)转换为客户端格式(target)
    ("openai-chat-completions", "anthropic"): (
        ToChatCompletionsConverter,
        ToAnthropicConverter,
    ),
    ("openai-response", "anthropic"): (ToResponseConverter, ToAnthropicConverter),
    ("openai-response", "openai-chat-completions"): (
        ToResponseConverter,
        ToChatCompletionsConverter,
    ),
    ("anthropic", "openai-chat-completions"): (
        ToAnthropicConverter,
        ToChatCompletionsConverter,
    ),
    ("anthropic", "openai-response"): (ToAnthropicConverter, ToResponseConverter),
    ("openai-chat-completions", "openai-response"): (
        ToChatCompletionsConverter,
        ToResponseConverter,
    ),
}


def filter_channels_by_conversion(
    channels: list[Channel], target_api_type: APIType
) -> list[Channel]:
    """按"是否允许跨格式转换"过滤渠道。

    同格式渠道（透传）始终通过。跨格式渠道按 channel.allow_format_conversion 决定，
    若该字段为 None 则回落到全局 settings.allow_format_conversion（默认 True）。
    """
    target = target_api_type.value
    global_allowed = get_setting("allow_format_conversion")
    if global_allowed is None:
        global_allowed = True
    result: list[Channel] = []
    for ch in channels:
        if ch.api_type.value == target:
            result.append(ch)
            continue
        allowed = ch.allow_format_conversion
        if allowed is None:
            allowed = global_allowed
        if allowed:
            result.append(ch)
    return result


def get_converter_and_upstream_type(
    channel: Channel, target_api_type: APIType
) -> tuple:
    """根据渠道类型和目标API类型，获取转换器和上游请求类型

    返回 (request_converter, response_converter, source_type)
    - request_converter: 用于把客户端格式转换为上游格式
    - response_converter: 用于把上游格式转换为客户端格式
    """
    source = channel.api_type.value
    target = target_api_type.value

    if source == target:
        return None, None, source

    converters = CONVERTER_MAP.get((source, target))
    if converters is None:
        raise ValueError(f"不支持的转换方向: {source} -> {target}")
    req_cls, resp_cls = converters
    return req_cls(), resp_cls(), source

async def prepare_openai_response_request_for_upstream(
    request_data: dict[str, Any],
    source_type: str,
    target_api_type: APIType,
) -> dict[str, Any]:
    """展开 Responses 本地历史，仅用于不支持 Responses 状态的上游。"""
    previous_response_id = request_data.get("previous_response_id")
    if (
        target_api_type != APIType.OPENAI_RESPONSE
        or source_type == APIType.OPENAI_RESPONSE.value
        or not previous_response_id
    ):
        return request_data

    conversation = await _responses_store.get_conversation(previous_response_id)
    if conversation is None:
        raise ValueError(f"Response {previous_response_id} not found")

    prepared = dict(request_data)
    prepared["input"] = list(
        conversation.get("messages", [])
    ) + response_input_to_items(request_data.get("input"))
    if not prepared.get("instructions") and conversation.get("instructions"):
        prepared["instructions"] = conversation["instructions"]
    prepared.pop("previous_response_id", None)
    return prepared


def response_input_to_items(input_data: Any) -> list[dict[str, Any]]:
    if input_data is None:
        return []
    if isinstance(input_data, str):
        return [{"role": "user", "content": input_data}]
    if isinstance(input_data, list):
        items: list[dict[str, Any]] = []
        for item in input_data:
            if isinstance(item, str):
                items.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                items.append(dict(item))
        return items
    return [{"role": "user", "content": str(input_data)}]


_filter_channels_by_conversion = filter_channels_by_conversion
_get_converter_and_upstream_type = get_converter_and_upstream_type
_prepare_openai_response_request_for_upstream = prepare_openai_response_request_for_upstream
_response_input_to_items = response_input_to_items
