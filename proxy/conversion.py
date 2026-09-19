from typing import Any

from config import get_setting
from converters.to_anthropic import ToAnthropicConverter
from converters.to_chat import ToChatCompletionsConverter
from converters.to_response import ToResponseConverter
from models.api_types import APIType
from models.channel import Channel, Endpoint
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


# 无原生匹配时的固定转格式尝试优先级：chat-completions > anthropic > responses
_ENDPOINT_ATTEMPT_PRIORITY: dict[str, int] = {
    APIType.OPENAI_CHAT.value: 0,
    APIType.ANTHROPIC.value: 1,
    APIType.OPENAI_RESPONSE.value: 2,
}
# 不在优先表内的格式（未来新增）排在所有已知格式之后
_UNRANKED_ENDPOINT_PRIORITY = 99


def _conversion_allowed(channel: Channel) -> bool:
    allowed = channel.allow_format_conversion
    if allowed is None:
        allowed = get_setting("allow_format_conversion")
    if allowed is None:
        allowed = True
    return allowed


def resolve_endpoint_attempts(channel: Channel, target_api_type: APIType) -> list[Endpoint]:
    """渠道内接入点尝试顺序（ADR-0006）。

    Native Match 优先直通；其余启用接入点按固定优先级排序供失败后
    渠道内转格式重试。渠道 allow_format_conversion=false 时只允许原生
    入口（原生失败即排除整个渠道）。返回空列表表示该渠道当前无法服务
    此请求（接入点全停用 / 无原生且禁止转换）。
    """
    target = target_api_type.value
    candidates = [ep for ep in channel.endpoints if ep.enabled]
    native = [ep for ep in candidates if ep.api_type.value == target]

    def by_priority(ep: Endpoint) -> int:
        return _ENDPOINT_ATTEMPT_PRIORITY.get(ep.api_type.value, _UNRANKED_ENDPOINT_PRIORITY)

    if native:
        if not _conversion_allowed(channel):
            return native
        rest = sorted((ep for ep in candidates if ep.api_type.value != target), key=by_priority)
        return native + rest
    if not candidates or not _conversion_allowed(channel):
        return []
    return sorted(candidates, key=by_priority)


def filter_channels_by_conversion(channels: list[Channel], target_api_type: APIType) -> list[Channel]:
    """按“渠道能否服务目标格式”过滤候选池（两级化后以接入点为准）。

    渠道存在可服务目标格式的启用接入点即通过；接入点全停用或无原生
    且禁止跨格式转换的渠道被剔除。
    """
    return [ch for ch in channels if resolve_endpoint_attempts(ch, target_api_type)]


def get_converter_for_endpoint(endpoint: Endpoint, target_api_type: APIType) -> tuple:
    """根据明确 Endpoint 与入口格式构造请求/响应转换器。"""
    source = endpoint.api_type.value
    target = target_api_type.value

    if source == target:
        return None, None, source

    converters = CONVERTER_MAP.get((source, target))
    if converters is None:
        raise ValueError(f"不支持的转换方向: {source} -> {target}")
    req_cls, resp_cls = converters
    return req_cls(), resp_cls(), source


def get_converter_and_upstream_type(channel: Channel, target_api_type: APIType) -> tuple:
    """根据渠道选定接入点与目标API类型，获取转换器和上游请求类型

    返回 (request_converter, response_converter, source_type)
    - request_converter: 用于把客户端格式转换为上游格式
    - response_converter: 用于把上游格式转换为客户端格式
    """
    return get_converter_for_endpoint(channel.selected_endpoint(), target_api_type)


async def prepare_openai_response_request_for_upstream(
    request_data: dict[str, Any],
    source_type: str,
    target_api_type: APIType,
) -> dict[str, Any]:
    """展开 Responses 本地历史，仅用于不支持 Responses 状态的上游。"""
    previous_response_id = request_data.get("previous_response_id")
    if target_api_type != APIType.OPENAI_RESPONSE or source_type == APIType.OPENAI_RESPONSE.value or not previous_response_id:
        return request_data

    conversation = await _responses_store.get_conversation(previous_response_id)
    if conversation is None:
        raise ValueError(f"Response {previous_response_id} not found")

    prepared = dict(request_data)
    prepared["input"] = list(conversation.get("messages", [])) + response_input_to_items(request_data.get("input"))
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


_get_converter_and_upstream_type = get_converter_and_upstream_type
_prepare_openai_response_request_for_upstream = prepare_openai_response_request_for_upstream
