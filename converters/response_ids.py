"""Responses 目标方向的 ID 伪造策略（ADR-0016 D2 二期：唯一住所）。

把上游 ID（Chat Completions / Anthropic）伪造（fabricate）为 Responses 协议形态的
``resp_*`` / ``msg_*`` / ``fc_*`` ID。改 ID 规则只改本模块，不要在状态机或转换器里
另起拷贝。
"""

import secrets


def make_response_id(upstream_id: str) -> str:
    if upstream_id.startswith("resp_"):
        return upstream_id
    suffix = upstream_id
    for prefix in ("chatcmpl_", "chatcmpl-", "cmpl_", "cmpl-"):
        if suffix.startswith(prefix):
            suffix = suffix[len(prefix) :]
            break
    suffix = suffix.replace("-", "_") or secrets.token_hex(12)
    return f"resp_{suffix}"


def make_message_id(response_id: str, upstream_id: str = "") -> str:
    if upstream_id and upstream_id.startswith("msg_"):
        return upstream_id
    suffix = response_id.removeprefix("resp_")
    return f"msg_{suffix}"


def make_function_call_id(call_id: str) -> str:
    if call_id.startswith("fc_"):
        return call_id
    suffix = call_id.removeprefix("call_") if call_id.startswith("call_") else call_id or secrets.token_hex(8)
    return f"fc_{suffix}"
