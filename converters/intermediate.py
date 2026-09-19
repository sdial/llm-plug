"""最小化中间条目（ADR-0016 D2 二期）。

每种源语法（Chat / Anthropic / Responses）各有一个解析模块
（``parsing_chat`` / ``parsing_anthropic`` / ``parsing_responses``），把源请求体
解析为本模块定义的中间条目；方向转换 = 解析 × 渲染的组合，渲染段留在各目标
转换器（to_chat / to_anthropic / to_response）。

明确不做全字段统一中间表示（IR 大手术是 ADR-0016 的 YAGNI）：中间条目只覆盖
已被成对复制的解析面——消息体 / content parts / 多模态块 / tools / tool_choice /
system / finish_reason。

## 消息条目（message entry）

```python
{
    "role": str,                  # 源消息角色（原样保留，含 developer 等非标准角色）
    "text": str | None,           # content 原样为字符串时的文本；列表 content 时为 None
    "parts": list[part],          # content 为列表时的分类条目（见下）
    "reasoning": str,             # Chat reasoning_content（Anthropic thinking 走 parts）
    "tool_calls": [               # assistant 侧工具调用（arguments 原样：对象或字符串）
        {"id": str, "name": str, "arguments": Any},
    ],
    "tool_results": [             # tool 结果（content 原样：字符串或块列表）
        {"tool_use_id": str, "content": Any, "is_error": bool, "parts": list | None},
    ],
    "item_type": str | None,      # 仅 Responses 源：原始 input item type
    "raw_content": Any,           # 仅 system/developer 类条目：源 content 原样保留
}                              # （to_anthropic 对 text dict 原样透传以保留扩展字段）
```

## content part 条目（kind 判别）

```python
{"kind": "text", "text": str}
{"kind": "image", "url": str}                          # http(s) URL
{"kind": "image", "media_type": str, "data": str}      # base64（Anthropic source）
{"kind": "image", "url": str, "detail": str}           # Responses input_image + detail
{"kind": "file", "file": {"file_id"|"filename"|"file_data": str}}
{"kind": "audio", "audio": {"data": str, "format": str}}
{"kind": "refusal", "text": str}
{"kind": "thinking", "text": str}                      # Anthropic thinking 块
{"kind": "document", "media_type"|"url"|"content": str}  # Anthropic document 块
{"kind": "search_result", "content": str | list}       # Anthropic search_result 块
{"kind": "redacted_thinking"}                          # Anthropic redacted_thinking 块
{"kind": "unknown", "block": dict}                     # 未识别块原样保留，渲染端决定降级
```

## 其余中间条目

- **工具条目**（Responses 扁平形态为基准）：
  ``{"type": "function", "name": str, "description": str, "parameters": dict | None, "strict"?: Any}``
  （``parameters`` 为 ``None`` 表示源未提供 schema；``strict`` 仅在源携带时存在）。
- **tool_choice 条目**：``"auto" | "none" | "required"`` 或
  ``{"type": "function", "name": str}``。
- **finish_reason 条目**：Chat finish_reason 词表
  （``stop / length / tool_calls / content_filter``）即中间词表。
"""

from typing import Any


def message_entry(
    role: str,
    *,
    text: str | None = None,
    parts: list[dict[str, Any]] | None = None,
    reasoning: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    tool_results: list[dict[str, Any]] | None = None,
    item_type: str | None = None,
    raw_content: Any = None,
) -> dict[str, Any]:
    """构造一条消息中间条目（形态见模块 docstring）。"""
    return {
        "role": role,
        "text": text,
        "parts": parts if parts is not None else [],
        "reasoning": reasoning,
        "tool_calls": tool_calls if tool_calls is not None else [],
        "tool_results": tool_results if tool_results is not None else [],
        "item_type": item_type,
        "raw_content": raw_content,
    }
