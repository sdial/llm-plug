"""请求 Conversion Plan：兼容性判断与转换执行的共同入口（ADR-0031）。"""

from __future__ import annotations

import copy
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from models.api_types import APIType
from models.upstream_profile import CapabilityState
from upstream_profile_resolver import ResolvedUpstreamProfile


class ConversionDisposition(str, Enum):
    EXACT = "exact"
    COMPATIBLE = "compatible"
    LOSSY = "lossy"
    IMPOSSIBLE = "impossible"


class ConversionDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    path: str
    feature: str
    disposition: ConversionDisposition
    capability: CapabilityState = CapabilityState.UNKNOWN
    message: str


class ConversionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    inbound_api_type: APIType
    upstream_api_type: APIType
    diagnostics: list[ConversionDiagnostic] = Field(default_factory=list)

    @property
    def executable(self) -> bool:
        return all(item.disposition in {ConversionDisposition.EXACT, ConversionDisposition.COMPATIBLE} for item in self.diagnostics)


class PreparedRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    payload: dict[str, Any]
    plan: ConversionPlan


class RequestConverter(Protocol):
    def convert_request(self, source_data: dict[str, Any], source_type: str = "") -> dict[str, Any]: ...


class IncompatibleRequestError(ValueError):
    def __init__(self, plan: ConversionPlan):
        self.plan = plan
        failure = next(
            (item for item in plan.diagnostics if item.disposition in {ConversionDisposition.LOSSY, ConversionDisposition.IMPOSSIBLE}),
            None,
        )
        self.code = failure.code if failure else "conversion_incompatible"
        self.path = failure.path if failure else "$"
        message = failure.message if failure else "The request cannot be converted losslessly"
        super().__init__(f"{self.code} at {self.path}: {message}")


class IncompatibleResponseError(Exception):
    def __init__(self, diagnostic: ConversionDiagnostic):
        self.diagnostic = diagnostic
        self.code = diagnostic.code
        self.path = diagnostic.path
        super().__init__(f"{diagnostic.code} at {diagnostic.path}: {diagnostic.message}")


_MODALITY_PART_TYPES = {
    "text": "text",
    "input_text": "text",
    "output_text": "text",
    "refusal": "text",
    "image_url": "image",
    "image": "image",
    "input_image": "image",
    "input_audio": "audio",
    "audio": "audio",
    "file": "file",
    "input_file": "file",
    "document": "file",
    "tool_use": "tools",
    "tool_result": "tools",
}

_EXPRESSIBLE_INPUTS = {
    APIType.OPENAI_CHAT: frozenset({"text", "image", "audio", "file", "tools"}),
    APIType.OPENAI_RESPONSE: frozenset({"text", "image", "audio", "file", "tools"}),
    APIType.ANTHROPIC: frozenset({"text", "image", "file", "tools"}),
}

_EXPRESSIBLE_OUTPUTS = {
    # 同格式由 prepare_response 的直通分支保留全部原生输出；跨格式只声明
    # converters 已有确定性、无损映射的输出，不因两端“各自支持”就臆造等价关系。
    APIType.OPENAI_CHAT: frozenset({"text", "reasoning"}),
    APIType.OPENAI_RESPONSE: frozenset({"text", "reasoning"}),
    APIType.ANTHROPIC: frozenset({"text", "reasoning"}),
}

_KNOWN_TOP_LEVEL_FIELDS = {
    APIType.OPENAI_CHAT: frozenset(
        {
            "model",
            "messages",
            "stream",
            "stream_options",
            "max_tokens",
            "max_completion_tokens",
            "temperature",
            "top_p",
            "stop",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "response_format",
            "reasoning_effort",
            "modalities",
            "audio",
            "metadata",
            "user",
            "service_tier",
            "seed",
            "n",
            "logprobs",
            "top_logprobs",
            "presence_penalty",
            "frequency_penalty",
        }
    ),
    APIType.OPENAI_RESPONSE: frozenset(
        {
            "model",
            "input",
            "instructions",
            "stream",
            "max_output_tokens",
            "temperature",
            "top_p",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "text",
            "reasoning",
            "metadata",
            "user",
            "service_tier",
            "store",
            "include",
            "truncation",
            "previous_response_id",
            "stop",
            "safety_identifier",
            "background",
            "conversation",
            "context_management",
        }
    ),
    APIType.ANTHROPIC: frozenset(
        {
            "model",
            "messages",
            "system",
            "stream",
            "max_tokens",
            "temperature",
            "top_p",
            "top_k",
            "stop_sequences",
            "tools",
            "tool_choice",
            "thinking",
            "metadata",
        }
    ),
}

# 这些集合描述的是本项目 converter 已经实现且经过验证的跨格式语义，
# 不是各厂商 API 的字段全集。字段虽然在源协议中合法，但不在对应集合中时，
# 必须在转换前拒绝，不能继续沿用 converter 里历史性的“忽略并记录日志”。
_CROSS_FORMAT_REQUEST_FIELDS = {
    (APIType.OPENAI_CHAT, APIType.ANTHROPIC): frozenset(
        {
            "model",
            "messages",
            "stream",
            "stream_options",
            "max_tokens",
            "max_completion_tokens",
            "temperature",
            "top_p",
            "stop",
            "tools",
            "tool_choice",
            "reasoning_effort",
            "metadata",
            "user",
        }
    ),
    (APIType.OPENAI_CHAT, APIType.OPENAI_RESPONSE): frozenset(
        {"model", "messages", "stream", "stream_options", "max_tokens", "temperature", "top_p", "tools", "tool_choice", "reasoning_effort"}
    ),
    (APIType.ANTHROPIC, APIType.OPENAI_CHAT): frozenset(
        {
            "model",
            "messages",
            "system",
            "stream",
            "max_tokens",
            "temperature",
            "top_p",
            "stop_sequences",
            "tools",
            "tool_choice",
            "thinking",
            "metadata",
        }
    ),
    (APIType.ANTHROPIC, APIType.OPENAI_RESPONSE): frozenset(
        {"model", "messages", "system", "stream", "max_tokens", "temperature", "top_p", "tools", "tool_choice", "thinking"}
    ),
    (APIType.OPENAI_RESPONSE, APIType.OPENAI_CHAT): frozenset(
        {
            "model",
            "input",
            "instructions",
            "stream",
            "max_output_tokens",
            "temperature",
            "top_p",
            "stop",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "text",
            "reasoning",
            "user",
            "safety_identifier",
            "store",
        }
    ),
    (APIType.OPENAI_RESPONSE, APIType.ANTHROPIC): frozenset(
        {
            "model",
            "input",
            "instructions",
            "stream",
            "max_output_tokens",
            "temperature",
            "top_p",
            "stop",
            "tools",
            "tool_choice",
            "reasoning",
            "store",
        }
    ),
}


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _responses_input_items(payload: dict[str, Any]):
    input_value = payload.get("input")
    if not isinstance(input_value, list):
        return
    for index, item in enumerate(input_value):
        if isinstance(item, dict) and isinstance(item.get("type"), str):
            yield f"$.input[{index}]", item["type"]


def _responses_tool_types(payload: dict[str, Any]):
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return
    for index, tool in enumerate(tools):
        if isinstance(tool, dict):
            yield f"$.tools[{index}]", str(tool.get("type", ""))


def _content_containers(payload: dict[str, Any], api_type: APIType):
    key = "input" if api_type is APIType.OPENAI_RESPONSE else "messages"
    container = payload.get(key)
    if not isinstance(container, list):
        return
    for item_index, item in enumerate(container):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part_index, part in enumerate(content):
            if isinstance(part, dict):
                yield f"$.{key}[{item_index}].content[{part_index}]", part


def _file_reference_is_private(part: dict[str, Any]) -> bool:
    if isinstance(part.get("file_id"), str):
        return True
    file_value = part.get("file")
    return isinstance(file_value, dict) and isinstance(file_value.get("file_id"), str)


def _portable_file_carrier(part: dict[str, Any], inbound_api_type: APIType, upstream_api_type: APIType) -> bool:
    if inbound_api_type is APIType.ANTHROPIC:
        source = part.get("source")
        if not isinstance(source, dict):
            return False
        source_type = source.get("type")
        # Chat 的标准 file_data 载体不能表达远程文件 URL；Responses 与 Anthropic 可以。
        return source_type == "base64" or source_type == "url" and upstream_api_type is not APIType.OPENAI_CHAT
    file_value = part.get("file") if isinstance(part.get("file"), dict) else part
    file_data = file_value.get("file_data")
    if isinstance(file_data, str) and file_data.startswith("data:"):
        return True
    file_url = file_value.get("file_url")
    return isinstance(file_url, str) and file_url.startswith(("http://", "https://")) and upstream_api_type is not APIType.OPENAI_CHAT


def _portable_image_carrier(part: dict[str, Any], inbound_api_type: APIType) -> bool:
    if inbound_api_type is APIType.ANTHROPIC:
        source = part.get("source")
        return isinstance(source, dict) and source.get("type") in {"base64", "url"}
    image_value = part.get("image_url")
    if isinstance(image_value, dict):
        image_value = image_value.get("url")
    return isinstance(image_value, str) and image_value.startswith(("data:", "http://", "https://"))


def _diagnose_request(
    payload: dict[str, Any],
    inbound_api_type: APIType,
    upstream_api_type: APIType,
    resolved: ResolvedUpstreamProfile,
) -> list[ConversionDiagnostic]:
    diagnostics: list[ConversionDiagnostic] = []
    same_format = inbound_api_type is upstream_api_type
    for path, part in _content_containers(payload, inbound_api_type) or ():
        part_type = str(part.get("type", ""))
        if part_type == "refusal" and not same_format:
            diagnostics.append(
                ConversionDiagnostic(
                    code="conversion_refusal_semantics",
                    path=path,
                    feature="refusal",
                    disposition=ConversionDisposition.LOSSY,
                    message="The target request format has no verified lossless mapping for refusal history semantics",
                )
            )
            continue
        modality = _MODALITY_PART_TYPES.get(part_type)
        if modality is None:
            if not same_format:
                diagnostics.append(
                    ConversionDiagnostic(
                        code="conversion_unknown_content_block",
                        path=f"{path}.type",
                        feature=part_type or "unknown-content",
                        disposition=ConversionDisposition.LOSSY,
                        message=f"The target format has no verified lossless mapping for the {part_type or 'unknown'} content block",
                    )
                )
            continue
        namespace = "features" if modality == "tools" else "input_modalities"
        capability = resolved.capabilities.state(namespace, modality)
        if capability is CapabilityState.UNSUPPORTED:
            diagnostics.append(
                ConversionDiagnostic(
                    code="upstream_capability_unsupported",
                    path=path,
                    feature=modality,
                    disposition=ConversionDisposition.IMPOSSIBLE,
                    capability=capability,
                    message=f"The active upstream profile explicitly does not support {modality} input",
                )
            )
            continue
        if modality not in _EXPRESSIBLE_INPUTS[upstream_api_type]:
            diagnostics.append(
                ConversionDiagnostic(
                    code="conversion_target_cannot_express",
                    path=path,
                    feature=modality,
                    disposition=ConversionDisposition.IMPOSSIBLE,
                    capability=capability,
                    message=f"{upstream_api_type.value} has no standard carrier for {modality} input",
                )
            )
            continue
        if modality == "file" and _file_reference_is_private(part) and not same_format:
            diagnostics.append(
                ConversionDiagnostic(
                    code="cross_provider_file_id",
                    path=path,
                    feature="file_id",
                    disposition=ConversionDisposition.IMPOSSIBLE,
                    capability=capability,
                    message="An upstream-private file_id cannot be converted across formats or file domains",
                )
            )
            continue
        if modality == "image" and isinstance(part.get("file_id"), str) and not same_format:
            diagnostics.append(
                ConversionDiagnostic(
                    code="cross_provider_file_id",
                    path=path,
                    feature="file_id",
                    disposition=ConversionDisposition.IMPOSSIBLE,
                    capability=capability,
                    message="An upstream-private image file_id cannot be converted across file domains",
                )
            )
            continue
        if modality == "image" and not same_format and not _portable_image_carrier(part, inbound_api_type):
            diagnostics.append(
                ConversionDiagnostic(
                    code="image_carrier_not_portable",
                    path=path,
                    feature="image",
                    disposition=ConversionDisposition.IMPOSSIBLE,
                    capability=capability,
                    message="The image has no standard URL or inline data representation that the target format can carry losslessly",
                )
            )
            continue
        if modality == "file" and not same_format and not _portable_file_carrier(part, inbound_api_type, upstream_api_type):
            diagnostics.append(
                ConversionDiagnostic(
                    code="file_carrier_not_portable",
                    path=path,
                    feature="file",
                    disposition=ConversionDisposition.IMPOSSIBLE,
                    capability=capability,
                    message="The file content has no standard URL or inline data representation that the target format can carry losslessly",
                )
            )
            continue
        diagnostics.append(
            ConversionDiagnostic(
                code="conversion_exact" if same_format else "conversion_compatible",
                path=path,
                feature=modality,
                disposition=ConversionDisposition.EXACT if same_format else ConversionDisposition.COMPATIBLE,
                capability=capability,
                message="Content can be passed through unchanged" if same_format else "Content has a verified lossless standard mapping",
            )
        )
    for field, feature in (
        ("tools", "tools"),
        ("parallel_tool_calls", "parallel_tool_calls"),
        ("response_format", "structured_output"),
        ("reasoning_effort", "reasoning"),
        ("thinking", "reasoning"),
        ("reasoning", "reasoning"),
    ):
        if field not in payload:
            continue
        capability = resolved.capabilities.state("features", feature)
        if capability is CapabilityState.UNSUPPORTED:
            diagnostics.append(
                ConversionDiagnostic(
                    code="upstream_capability_unsupported",
                    path=f"$.{field}",
                    feature=feature,
                    disposition=ConversionDisposition.IMPOSSIBLE,
                    capability=capability,
                    message=f"The active upstream profile explicitly does not support {feature}",
                )
            )
    if not same_format:
        supported_fields = _CROSS_FORMAT_REQUEST_FIELDS[(inbound_api_type, upstream_api_type)]
        for field in (payload.keys() & _KNOWN_TOP_LEVEL_FIELDS[inbound_api_type]) - supported_fields:
            diagnostics.append(
                ConversionDiagnostic(
                    code="conversion_unmapped_field",
                    path=f"$.{field}",
                    feature=field,
                    disposition=ConversionDisposition.LOSSY,
                    message=f"{inbound_api_type.value} has no lossless mapping for this field to {upstream_api_type.value}",
                )
            )
        for field in payload.keys() - _KNOWN_TOP_LEVEL_FIELDS[inbound_api_type]:
            diagnostics.append(
                ConversionDiagnostic(
                    code="conversion_unknown_field",
                    path=f"$.{field}",
                    feature=field,
                    disposition=ConversionDisposition.LOSSY,
                    message="Cross-format conversion has no lossless mapping for this unknown field",
                )
            )
        if inbound_api_type is APIType.ANTHROPIC and _contains_key(payload, "cache_control"):
            diagnostics.append(
                ConversionDiagnostic(
                    code="conversion_unmapped_field",
                    path="$..cache_control",
                    feature="cache_control",
                    disposition=ConversionDisposition.LOSSY,
                    message="The target format has no equivalent semantics for Anthropic cache_control",
                )
            )
        metadata = payload.get("metadata")
        if "metadata" in supported_fields and isinstance(metadata, dict) and set(metadata) - {"user_id"}:
            diagnostics.append(
                ConversionDiagnostic(
                    code="conversion_metadata_not_portable",
                    path="$.metadata",
                    feature="metadata",
                    disposition=ConversionDisposition.LOSSY,
                    message="Cross-format conversion can map only metadata.user_id losslessly",
                )
            )
        if inbound_api_type is APIType.OPENAI_RESPONSE:
            for path, item_type in _responses_input_items(payload) or ():
                if item_type not in {"message", "function_call", "function_call_output"}:
                    diagnostics.append(
                        ConversionDiagnostic(
                            code="conversion_unmapped_input_item",
                            path=f"{path}.type",
                            feature=item_type,
                            disposition=ConversionDisposition.LOSSY,
                            message="The target format has no lossless historical semantics for this Responses input item",
                        )
                    )
            for path, tool_type in _responses_tool_types(payload) or ():
                if tool_type != "function":
                    diagnostics.append(
                        ConversionDiagnostic(
                            code="conversion_unmapped_tool_type",
                            path=f"{path}.type",
                            feature=tool_type or "unknown-tool",
                            disposition=ConversionDisposition.LOSSY,
                            message="The target format has no lossless mapping for this Responses tool type",
                        )
                    )
    if not diagnostics:
        diagnostics.append(
            ConversionDiagnostic(
                code="conversion_exact" if same_format else "conversion_compatible",
                path="$",
                feature="request",
                disposition=ConversionDisposition.EXACT if same_format else ConversionDisposition.COMPATIBLE,
                message="The request does not need rewriting" if same_format else "The request can be converted losslessly",
            )
        )
    return diagnostics


def prepare_request(
    payload: dict[str, Any],
    inbound_api_type: APIType,
    upstream_api_type: APIType,
    resolved: ResolvedUpstreamProfile,
    converter: RequestConverter | None,
) -> PreparedRequest:
    diagnostics = _diagnose_request(payload, inbound_api_type, upstream_api_type, resolved)
    plan = ConversionPlan(inbound_api_type=inbound_api_type, upstream_api_type=upstream_api_type, diagnostics=diagnostics)
    if not plan.executable:
        raise IncompatibleRequestError(plan)
    prepared = copy.deepcopy(payload)
    if converter is not None:
        prepared = converter.convert_request(prepared, inbound_api_type.value)
    if resolved.normalize_developer_role and isinstance(prepared.get("messages"), list):
        for message in prepared["messages"]:
            if isinstance(message, dict) and message.get("role") == "developer":
                message["role"] = "system"
    return PreparedRequest(payload=prepared, plan=plan)


def _response_features(payload: dict[str, Any], api_type: APIType):
    if api_type is APIType.ANTHROPIC:
        for index, part in enumerate(payload.get("content", [])):
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type", ""))
            if part_type == "text":
                yield f"$.content[{index}]", "text", True
            elif part_type == "thinking":
                yield f"$.content[{index}]", "reasoning", True
            elif part_type == "tool_use":
                yield f"$.content[{index}]", "tools", True
            elif part_type == "redacted_thinking":
                yield f"$.content[{index}]", "redacted-reasoning", False
            else:
                yield f"$.content[{index}]", _MODALITY_PART_TYPES.get(part_type, part_type or "unknown-content"), False
        return
    if api_type is APIType.OPENAI_CHAT:
        for choice_index, choice in enumerate(payload.get("choices", [])):
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                continue
            message = choice["message"]
            if message.get("audio") is not None:
                yield f"$.choices[{choice_index}].message.audio", "audio", True
            content = message.get("content")
            if isinstance(content, list):
                for part_index, part in enumerate(content):
                    if not isinstance(part, dict):
                        continue
                    part_type = str(part.get("type", ""))
                    known = part_type in {"text", "refusal"} or part_type in _MODALITY_PART_TYPES
                    yield (
                        f"$.choices[{choice_index}].message.content[{part_index}]",
                        "refusal" if part_type == "refusal" else _MODALITY_PART_TYPES.get(part_type, "text" if part_type == "text" else part_type),
                        known,
                    )
            elif content is not None:
                yield f"$.choices[{choice_index}].message.content", "text", True
            if message.get("refusal"):
                yield f"$.choices[{choice_index}].message.refusal", "refusal", True
            if message.get("tool_calls"):
                yield f"$.choices[{choice_index}].message.tool_calls", "tools", True
        return
    for index, item in enumerate(payload.get("output", [])):
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", ""))
        if item_type == "message":
            content = item.get("content")
            if not isinstance(content, list):
                yield f"$.output[{index}].content", "unknown-output", False
                continue
            for content_index, part in enumerate(content):
                if not isinstance(part, dict):
                    yield f"$.output[{index}].content[{content_index}]", "unknown-output", False
                    continue
                part_type = str(part.get("type", ""))
                if part_type == "output_text":
                    yield f"$.output[{index}].content[{content_index}]", "text", True
                elif part_type == "refusal":
                    yield f"$.output[{index}].content[{content_index}]", "refusal", True
                else:
                    feature = _MODALITY_PART_TYPES.get(part_type, part_type or "unknown-output")
                    yield f"$.output[{index}].content[{content_index}]", feature, False
        elif item_type == "reasoning":
            yield f"$.output[{index}]", "reasoning", True
        elif item_type in {"function_call", "function_call_output"}:
            yield f"$.output[{index}]", "tools", True
        elif item_type == "image_generation_call":
            yield f"$.output[{index}]", "image", True
        elif "audio" in item_type:
            yield f"$.output[{index}]", "audio", True
        elif "file" in item_type:
            yield f"$.output[{index}]", "file", True
        else:
            yield f"$.output[{index}]", item_type or "unknown-output", False


def prepare_response(
    payload: dict[str, Any],
    upstream_api_type: APIType,
    inbound_api_type: APIType,
    converter: Any | None,
) -> dict[str, Any]:
    """响应转换前拒绝目标格式无法表达或没有已验证映射的输出。"""
    same_format = upstream_api_type is inbound_api_type
    if not same_format:
        if upstream_api_type is APIType.OPENAI_CHAT and inbound_api_type is APIType.ANTHROPIC:
            choices = payload.get("choices")
            if isinstance(choices, list) and len(choices) > 1:
                raise IncompatibleResponseError(
                    ConversionDiagnostic(
                        code="response_multiple_choices",
                        path="$.choices",
                        feature="multiple-choices",
                        disposition=ConversionDisposition.LOSSY,
                        message="Anthropic Messages cannot represent multiple Chat choices losslessly",
                    )
                )
        for path, feature, known in _response_features(payload, upstream_api_type) or ():
            if not known:
                raise IncompatibleResponseError(
                    ConversionDiagnostic(
                        code="response_unknown_output",
                        path=path,
                        feature=feature,
                        disposition=ConversionDisposition.LOSSY,
                        message="The target format has no verified lossless mapping for this output item",
                    )
                )
            pair_features = {"tools"}
            if upstream_api_type is APIType.OPENAI_CHAT and inbound_api_type is APIType.OPENAI_RESPONSE:
                pair_features.add("refusal")
            if feature not in pair_features and feature not in _EXPRESSIBLE_OUTPUTS[inbound_api_type]:
                raise IncompatibleResponseError(
                    ConversionDiagnostic(
                        code="response_target_cannot_express",
                        path=path,
                        feature=feature,
                        disposition=ConversionDisposition.IMPOSSIBLE,
                        message=f"{inbound_api_type.value} has no standard carrier for {feature} output",
                    )
                )
    return converter.convert_response(payload, upstream_api_type.value) if converter is not None else payload


def _stream_response_features(chunk: dict[str, Any], api_type: APIType):
    """提取单个流事件中已经实际出现、可能无法跨格式表达的输出。"""
    if api_type is APIType.ANTHROPIC:
        if chunk.get("type") == "content_block_start" and isinstance(chunk.get("content_block"), dict):
            block_type = str(chunk["content_block"].get("type", ""))
            if block_type == "redacted_thinking":
                yield "$.content_block", "redacted-reasoning", False
            elif block_type not in {"text", "thinking", "tool_use"}:
                yield "$.content_block", _MODALITY_PART_TYPES.get(block_type, block_type or "unknown-output"), False
        return
    if api_type is APIType.OPENAI_CHAT:
        for choice_index, choice in enumerate(chunk.get("choices", [])):
            if not isinstance(choice, dict) or not isinstance(choice.get("delta"), dict):
                continue
            delta = choice["delta"]
            if delta.get("audio") is not None:
                yield f"$.choices[{choice_index}].delta.audio", "audio", True
            content = delta.get("content")
            if isinstance(content, list):
                for part_index, part in enumerate(content):
                    if not isinstance(part, dict):
                        continue
                    part_type = str(part.get("type", ""))
                    known = part_type in {"text", "refusal"} or part_type in _MODALITY_PART_TYPES
                    feature = _MODALITY_PART_TYPES.get(part_type, "text" if part_type in {"text", "refusal"} else part_type)
                    yield f"$.choices[{choice_index}].delta.content[{part_index}]", feature, known
        return
    event_type = str(chunk.get("type", ""))
    if event_type in {"response.output_item.added", "response.output_item.done"} and isinstance(chunk.get("item"), dict):
        item_type = str(chunk["item"].get("type", ""))
        if item_type not in {"message", "reasoning", "function_call"}:
            if item_type == "image_generation_call":
                yield "$.item.type", "image", True
            elif "audio" in item_type:
                yield "$.item.type", "audio", True
            elif "file" in item_type:
                yield "$.item.type", "file", True
            else:
                yield "$.item.type", item_type or "unknown-output", False
        return
    if "image_generation" in event_type:
        yield "$.type", "image", True
    elif event_type.startswith("response.audio") or ".audio." in event_type:
        yield "$.type", "audio", True
    elif "file" in event_type and event_type.startswith("response."):
        yield "$.type", "file", True


def validate_stream_response_chunk(
    chunk: dict[str, Any],
    upstream_api_type: APIType,
    inbound_api_type: APIType,
) -> None:
    """在 converter 消费流事件前验证实际输出是否能被入口协议表达。"""
    if upstream_api_type is inbound_api_type:
        return
    for path, feature, known in _stream_response_features(chunk, upstream_api_type) or ():
        if not known:
            raise IncompatibleResponseError(
                ConversionDiagnostic(
                    code="response_unknown_output",
                    path=path,
                    feature=feature,
                    disposition=ConversionDisposition.LOSSY,
                    message="The target format has no verified lossless mapping for this streaming output item",
                )
            )
        if feature not in _EXPRESSIBLE_OUTPUTS[inbound_api_type]:
            raise IncompatibleResponseError(
                ConversionDiagnostic(
                    code="response_target_cannot_express",
                    path=path,
                    feature=feature,
                    disposition=ConversionDisposition.IMPOSSIBLE,
                    message=f"{inbound_api_type.value} has no standard carrier for {feature} output",
                )
            )
