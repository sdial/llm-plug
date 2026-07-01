from models.api_types import APIType
from models.channel import Channel
from url_builder import append_query, build_models_url, build_upstream_url


def test_build_upstream_url_uses_base_url_fallback_for_standard_chat_endpoint():
    channel = Channel(
        name="OpenAI",
        api_type=APIType.OPENAI_CHAT,
        base_url="https://api.openai.com",
        api_key="sk-test",
    )

    assert build_upstream_url(channel) == "https://api.openai.com/v1/chat/completions"


def test_build_upstream_url_uses_endpoint_url_advanced_override():
    channel = Channel(
        name="Custom",
        api_type=APIType.OPENAI_CHAT,
        base_url="https://api.example.com",
        endpoint_url="https://gateway.example.com/custom/chat",
        api_key="sk-test",
    )

    assert build_upstream_url(channel) == "https://gateway.example.com/custom/chat"


def test_build_upstream_url_falls_back_to_base_url_when_endpoint_url_is_blank():
    channel = Channel(
        name="Custom",
        api_type=APIType.OPENAI_RESPONSE,
        base_url="https://api.example.com/v1",
        endpoint_url=" ",
        api_key="sk-test",
    )

    assert build_upstream_url(channel) == "https://api.example.com/v1/responses"


def test_build_models_url_uses_advanced_models_url_before_base_url():
    assert (
        build_models_url(
            base_url="https://api.example.com",
            models_url="https://models.example.com/list",
        )
        == "https://models.example.com/list"
    )


def test_build_models_url_falls_back_to_base_url_default_models_path():
    assert (
        build_models_url("https://api.example.com/v1")
        == "https://api.example.com/v1/models"
    )


def test_append_query_merges_existing_and_forwarded_query_parameters():
    assert (
        append_query(
            "https://api.example.com/v1/responses?beta=true", "timeout=30&debug="
        )
        == "https://api.example.com/v1/responses?beta=true&timeout=30&debug="
    )
