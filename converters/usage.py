"""Token usage 字段在 Anthropic / OpenAI 两种语义之间的映射。

Anthropic:
  - input_tokens 不含 cache
  - cache_creation_input_tokens / cache_read_input_tokens 独立计费字段
  - output_tokens 含 thinking

OpenAI:
  - prompt_tokens 含全部输入（包括缓存命中）
  - prompt_tokens_details.cached_tokens 是 prompt_tokens 的子集
  - completion_tokens 含 reasoning
  - completion_tokens_details.reasoning_tokens 是 completion_tokens 的子集
"""
from __future__ import annotations
from typing import Any
from loguru import logger


def _read_int(d: dict[str, Any] | None, key: str) -> int:
    if not isinstance(d, dict):
        return 0
    value = d.get(key, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def anthropic_to_openai_chat(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Anthropic usage → OpenAI Chat Completions usage."""
    if not isinstance(usage, dict):
        usage = {}
    inp = _read_int(usage, "input_tokens")
    cc = _read_int(usage, "cache_creation_input_tokens")
    cr = _read_int(usage, "cache_read_input_tokens")
    out = _read_int(usage, "output_tokens")
    prompt = inp + cc + cr
    return {
        "prompt_tokens": prompt,
        "completion_tokens": out,
        "total_tokens": prompt + out,
        "prompt_tokens_details": {"cached_tokens": cr},
    }


def anthropic_to_openai_response(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Anthropic usage → OpenAI Response usage."""
    if not isinstance(usage, dict):
        usage = {}
    inp = _read_int(usage, "input_tokens")
    cc = _read_int(usage, "cache_creation_input_tokens")
    cr = _read_int(usage, "cache_read_input_tokens")
    out = _read_int(usage, "output_tokens")
    total_input = inp + cc + cr
    return {
        "input_tokens": total_input,
        "output_tokens": out,
        "total_tokens": total_input + out,
        "input_tokens_details": {"cached_tokens": cr},
    }


def openai_chat_to_anthropic(usage: dict[str, Any] | None) -> dict[str, Any]:
    """OpenAI Chat usage → Anthropic usage。OpenAI 不区分 cache_creation。"""
    if not isinstance(usage, dict):
        usage = {}
    pt = _read_int(usage, "prompt_tokens")
    ct = _read_int(usage, "completion_tokens")
    cached = _read_int(usage.get("prompt_tokens_details"), "cached_tokens")
    if cached > pt:
        logger.warning(
            "openai_chat_to_anthropic: cached_tokens (%d) > prompt_tokens (%d), clamping input_tokens to 0",
            cached, pt,
        )
    return {
        "input_tokens": max(pt - cached, 0),
        "output_tokens": ct,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached,
    }


def openai_response_to_anthropic(usage: dict[str, Any] | None) -> dict[str, Any]:
    """OpenAI Response usage → Anthropic usage。"""
    if not isinstance(usage, dict):
        usage = {}
    inp = _read_int(usage, "input_tokens")
    out = _read_int(usage, "output_tokens")
    cached = _read_int(usage.get("input_tokens_details"), "cached_tokens")
    if cached > inp:
        logger.warning(
            "openai_response_to_anthropic: cached_tokens (%d) > input_tokens (%d), clamping to 0",
            cached, inp,
        )
    return {
        "input_tokens": max(inp - cached, 0),
        "output_tokens": out,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached,
    }
