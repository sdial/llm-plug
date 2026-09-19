import pytest

from conversion_plan import (
    ConversionDisposition,
    IncompatibleRequestError,
    IncompatibleResponseError,
    prepare_request,
    prepare_response,
    validate_stream_response_chunk,
)
from models.api_types import APIType
from models.upstream_profile import CapabilityMatrix, CapabilityState
from upstream_profile_resolver import ResolvedUpstreamProfile


class EchoConverter:
    def convert_request(self, source_data, source_type=""):
        return {**source_data, "converted_from": source_type}

    def convert_response(self, source_data, source_type=""):
        return {**source_data, "converted_from": source_type}


def _resolved(api_type: APIType, capabilities: CapabilityMatrix | None = None) -> ResolvedUpstreamProfile:
    return ResolvedUpstreamProfile(
        upstream_profile_id="generic",
        catalog_revision="builtin-2",
        api_type=api_type,
        model_id="m",
        capabilities=capabilities or CapabilityMatrix(),
    )


def test_same_format_preserves_unknown_fields_exactly():
    payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "future_option": {"enabled": True}}
    prepared = prepare_request(payload, APIType.OPENAI_CHAT, APIType.OPENAI_CHAT, _resolved(APIType.OPENAI_CHAT), None)
    assert prepared.payload == payload
    assert prepared.plan.executable is True
    assert prepared.plan.diagnostics[0].disposition is ConversionDisposition.EXACT


def test_unknown_image_capability_is_forwarded_when_target_format_can_express_it():
    payload = {
        "model": "m",
        "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example/image.png"}}]}],
    }
    prepared = prepare_request(
        payload,
        APIType.OPENAI_CHAT,
        APIType.OPENAI_RESPONSE,
        _resolved(APIType.OPENAI_RESPONSE),
        EchoConverter(),
    )
    assert prepared.payload["converted_from"] == APIType.OPENAI_CHAT.value
    assert prepared.plan.diagnostics[0].capability is CapabilityState.UNKNOWN
    assert prepared.plan.diagnostics[0].disposition is ConversionDisposition.COMPATIBLE


def test_explicit_unsupported_modality_is_rejected():
    payload = {
        "model": "m",
        "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "AA=="}}]}],
    }
    resolved = _resolved(
        APIType.OPENAI_CHAT,
        CapabilityMatrix(input_modalities={"audio": CapabilityState.UNSUPPORTED}),
    )
    with pytest.raises(IncompatibleRequestError) as caught:
        prepare_request(payload, APIType.OPENAI_CHAT, APIType.OPENAI_CHAT, resolved, None)
    assert caught.value.code == "upstream_capability_unsupported"
    assert caught.value.path == "$.messages[0].content[0]"


def test_audio_to_anthropic_is_impossible_even_when_capability_is_unknown():
    payload = {
        "model": "m",
        "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "AA=="}}]}],
    }
    with pytest.raises(IncompatibleRequestError) as caught:
        prepare_request(payload, APIType.OPENAI_CHAT, APIType.ANTHROPIC, _resolved(APIType.ANTHROPIC), EchoConverter())
    assert caught.value.code == "conversion_target_cannot_express"


def test_cross_format_private_file_id_is_impossible():
    payload = {
        "model": "m",
        "input": [{"role": "user", "content": [{"type": "input_file", "file_id": "file_123"}]}],
    }
    with pytest.raises(IncompatibleRequestError) as caught:
        prepare_request(payload, APIType.OPENAI_RESPONSE, APIType.OPENAI_CHAT, _resolved(APIType.OPENAI_CHAT), EchoConverter())
    assert caught.value.code == "cross_provider_file_id"


def test_cross_format_image_response_rejected_when_target_cannot_express_it():
    payload = {"output": [{"type": "image_generation_call", "result": "AA=="}]}
    with pytest.raises(IncompatibleResponseError) as caught:
        prepare_response(payload, APIType.OPENAI_RESPONSE, APIType.ANTHROPIC, EchoConverter())
    assert caught.value.code == "response_target_cannot_express"
    assert caught.value.path == "$.output[0]"


def test_same_format_response_preserves_unknown_output():
    payload = {"output": [{"type": "future_output", "value": 1}]}
    assert prepare_response(payload, APIType.OPENAI_RESPONSE, APIType.OPENAI_RESPONSE, None) == payload


def test_cross_format_unknown_response_output_is_rejected():
    payload = {"output": [{"type": "future_output", "value": 1}]}
    with pytest.raises(IncompatibleResponseError) as caught:
        prepare_response(payload, APIType.OPENAI_RESPONSE, APIType.OPENAI_CHAT, EchoConverter())
    assert caught.value.code == "response_unknown_output"


def test_stream_image_event_is_rejected_before_cross_format_converter():
    with pytest.raises(IncompatibleResponseError) as caught:
        validate_stream_response_chunk(
            {"type": "response.image_generation_call.partial_image", "partial_image_b64": "AA=="},
            APIType.OPENAI_RESPONSE,
            APIType.OPENAI_CHAT,
        )
    assert caught.value.code == "response_target_cannot_express"


@pytest.mark.parametrize("inbound", list(APIType))
@pytest.mark.parametrize("upstream", list(APIType))
def test_basic_text_request_plan_covers_full_three_by_three_matrix(inbound, upstream):
    payloads = {
        APIType.OPENAI_CHAT: {"model": "m", "messages": [{"role": "user", "content": "hello"}]},
        APIType.OPENAI_RESPONSE: {"model": "m", "input": [{"role": "user", "content": "hello"}]},
        APIType.ANTHROPIC: {"model": "m", "max_tokens": 32, "messages": [{"role": "user", "content": "hello"}]},
    }
    prepared = prepare_request(
        payloads[inbound],
        inbound,
        upstream,
        _resolved(upstream),
        None if inbound is upstream else EchoConverter(),
    )
    assert prepared.plan.executable is True
    assert prepared.plan.inbound_api_type is inbound
    assert prepared.plan.upstream_api_type is upstream


def test_file_without_portable_content_is_rejected_before_converter():
    payload = {
        "model": "m",
        "messages": [{"role": "user", "content": [{"type": "file", "file": {"filename": "name-only.pdf"}}]}],
    }
    with pytest.raises(IncompatibleRequestError) as caught:
        prepare_request(payload, APIType.OPENAI_CHAT, APIType.ANTHROPIC, _resolved(APIType.ANTHROPIC), EchoConverter())
    assert caught.value.code == "file_carrier_not_portable"


def test_anthropic_base64_document_is_portable_to_chat():
    payload = {
        "model": "m",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {"type": "base64", "media_type": "application/pdf", "data": "AA=="},
                    }
                ],
            }
        ],
    }
    prepared = prepare_request(payload, APIType.ANTHROPIC, APIType.OPENAI_CHAT, _resolved(APIType.OPENAI_CHAT), EchoConverter())
    assert prepared.plan.executable is True


def test_anthropic_document_url_is_not_portable_to_chat_file_data():
    payload = {
        "model": "m",
        "messages": [{"role": "user", "content": [{"type": "document", "source": {"type": "url", "url": "https://example/x.pdf"}}]}],
    }
    with pytest.raises(IncompatibleRequestError) as caught:
        prepare_request(payload, APIType.ANTHROPIC, APIType.OPENAI_CHAT, _resolved(APIType.OPENAI_CHAT), EchoConverter())
    assert caught.value.code == "file_carrier_not_portable"


@pytest.mark.parametrize(
    ("inbound", "upstream", "field", "value"),
    [
        (APIType.OPENAI_CHAT, APIType.ANTHROPIC, "seed", 7),
        (APIType.OPENAI_CHAT, APIType.OPENAI_RESPONSE, "stop", ["END"]),
        (APIType.ANTHROPIC, APIType.OPENAI_CHAT, "top_k", 10),
        (APIType.ANTHROPIC, APIType.OPENAI_RESPONSE, "stop_sequences", ["END"]),
        (APIType.OPENAI_RESPONSE, APIType.OPENAI_CHAT, "service_tier", "priority"),
        (APIType.OPENAI_RESPONSE, APIType.ANTHROPIC, "parallel_tool_calls", True),
    ],
)
def test_known_but_unmapped_cross_format_fields_are_rejected(inbound, upstream, field, value):
    payloads = {
        APIType.OPENAI_CHAT: {"model": "m", "messages": [{"role": "user", "content": "hello"}]},
        APIType.OPENAI_RESPONSE: {"model": "m", "input": [{"role": "user", "content": "hello"}]},
        APIType.ANTHROPIC: {"model": "m", "max_tokens": 32, "messages": [{"role": "user", "content": "hello"}]},
    }
    payloads[inbound][field] = value
    with pytest.raises(IncompatibleRequestError) as caught:
        prepare_request(payloads[inbound], inbound, upstream, _resolved(upstream), EchoConverter())
    assert caught.value.code == "conversion_unmapped_field"
    assert caught.value.path == f"$.{field}"


def test_nested_anthropic_cache_control_is_rejected_cross_format():
    payload = {
        "model": "m",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}]}],
    }
    with pytest.raises(IncompatibleRequestError) as caught:
        prepare_request(payload, APIType.ANTHROPIC, APIType.OPENAI_CHAT, _resolved(APIType.OPENAI_CHAT), EchoConverter())
    assert caught.value.code == "conversion_unmapped_field"
    assert caught.value.path == "$..cache_control"


def test_responses_hosted_tool_is_rejected_cross_format():
    payload = {
        "model": "m",
        "input": "find it",
        "tools": [{"type": "web_search_preview"}],
    }
    with pytest.raises(IncompatibleRequestError) as caught:
        prepare_request(payload, APIType.OPENAI_RESPONSE, APIType.OPENAI_CHAT, _resolved(APIType.OPENAI_CHAT), EchoConverter())
    assert caught.value.code == "conversion_unmapped_tool_type"


def test_responses_reasoning_history_is_not_silently_dropped():
    payload = {
        "model": "m",
        "input": [{"type": "reasoning", "id": "rs_1", "summary": []}],
    }
    with pytest.raises(IncompatibleRequestError) as caught:
        prepare_request(payload, APIType.OPENAI_RESPONSE, APIType.OPENAI_CHAT, _resolved(APIType.OPENAI_CHAT), EchoConverter())
    assert caught.value.code == "conversion_unmapped_input_item"


def test_responses_nested_refusal_output_is_rejected_for_anthropic():
    payload = {
        "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}],
    }
    with pytest.raises(IncompatibleResponseError) as caught:
        prepare_response(payload, APIType.OPENAI_RESPONSE, APIType.ANTHROPIC, EchoConverter())
    assert caught.value.code == "response_target_cannot_express"
    assert caught.value.path == "$.output[0].content[0]"


def test_chat_refusal_output_is_preserved_when_target_is_responses():
    payload = {
        "choices": [{"message": {"role": "assistant", "content": None, "refusal": "no"}, "finish_reason": "stop"}],
    }
    converted = prepare_response(payload, APIType.OPENAI_CHAT, APIType.OPENAI_RESPONSE, EchoConverter())
    assert converted["converted_from"] == APIType.OPENAI_CHAT.value


def test_stream_output_item_with_nontext_output_is_rejected():
    with pytest.raises(IncompatibleResponseError) as caught:
        validate_stream_response_chunk(
            {"type": "response.output_item.added", "item": {"type": "image_generation_call"}},
            APIType.OPENAI_RESPONSE,
            APIType.OPENAI_CHAT,
        )
    assert caught.value.code == "response_target_cannot_express"
    assert caught.value.path == "$.item.type"
