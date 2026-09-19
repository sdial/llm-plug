"""OpenAI Responses 源语法解析（ADR-0016 D2 二期）。

把 Responses 请求体解析为最小化中间条目（形态见 ``converters/intermediate.py``），
供 to_chat / to_anthropic 两个方向共用——原先 to_chat 与 to_anthropic 各自解析
一遍 Responses 语法的成对复制段随本模块归零。渲染段（中间条目 → 目标语法）
留在各目标转换器。
"""

from typing import Any

from converters.intermediate import message_entry

# Responses 托管工具类型：仅 Chat 目标方向需要显式丢弃并告警（目标端策略），
# 解析层负责把它们与 function 工具分类开。
HOSTED_RESPONSE_TOOL_TYPES = {
    "web_search",
    "web_search_preview",
    "file_search",
    "code_interpreter",
    "computer_use",
    "image_generation",
    "mcp",
}


def parse_responses_tools(tools: list) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Responses tools → (中间工具条目, 托管工具类型列表, 不支持的工具类型列表)。

    托管工具与未支持类型的处置策略（告警 / 抛错 / 静默跳过）由目标渲染端决定：
    Chat 目标告警并丢弃托管工具、对未支持类型抛 ValueError；Anthropic 目标
    静默忽略两者（与既有行为一致）。
    """
    entries: list[dict[str, Any]] = []
    hosted_types: list[str] = []
    unsupported_types: list[str] = []
    for tool in tools:
        tool_type = tool.get("type")
        if tool_type in HOSTED_RESPONSE_TOOL_TYPES:
            hosted_types.append(tool_type)
            continue
        if tool_type != "function":
            unsupported_types.append(tool_type)
            continue
        entry = {
            "type": "function",
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters", {}),
        }
        if tool.get("strict") is not None:
            entry["strict"] = tool["strict"]
        entries.append(entry)
    return entries, hosted_types, unsupported_types


def parse_responses_tool_choice(value: Any) -> str | dict[str, Any]:
    """Responses tool_choice → 中间条目；不支持的取值抛 ValueError。

    注：Responses→Anthropic 方向原先对不支持的取值静默丢弃，收敛后统一显式
    报错（无效输入应尽早失败，而非让上游自行拒绝）。
    """
    if isinstance(value, str):
        if value in ("auto", "none", "required"):
            return value
        raise ValueError(f"Unsupported Responses tool_choice value: {value}")
    if isinstance(value, dict):
        choice_type = value.get("type")
        if choice_type == "function":
            name = value.get("name") or value.get("function", {}).get("name")
            if not name:
                raise ValueError("Responses function tool_choice requires a function name")
            return {"type": "function", "name": name}
        if choice_type in ("auto", "none", "required"):
            return choice_type
    raise ValueError(f"Unsupported Responses tool_choice value: {value}")


def parse_responses_finish(status: Any, output: Any) -> str:
    """Responses (status, output) → 中间 finish 条目。

    incomplete 优先于 function_call 推断（与原 to_chat / to_anthropic /
    to_chat 流式三处推断一致）；无 function_call 输出时按 stop 兜底。
    """
    if status == "incomplete":
        return "length"
    for item in output or []:
        if isinstance(item, dict) and item.get("type") == "function_call":
            return "tool_calls"
    return "stop"


def parse_responses_content_parts(content: list) -> list[dict[str, Any]]:
    """Responses content 列表 → kind part 条目（形态见 intermediate.py）。"""
    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            parts.append({"kind": "text", "text": part})
            continue
        if not isinstance(part, dict):
            parts.append({"kind": "text", "text": str(part)})
            continue
        part_type = part.get("type")
        if part_type in ("input_text", "output_text", "text"):
            parts.append({"kind": "text", "text": part.get("text", "")})
        elif part_type == "input_image":
            image_url = part.get("image_url") or part.get("url")
            url = ""
            detail = None
            if isinstance(image_url, dict):
                url = image_url.get("url", "")
                detail = image_url.get("detail")
            elif isinstance(image_url, str):
                url = image_url
            entry: dict[str, Any] = {"kind": "image", "url": url}
            if detail is None and part.get("detail") is not None:
                detail = part.get("detail")
            if detail is not None:
                entry["detail"] = detail
            parts.append(entry)
        elif part_type == "input_file":
            fields = {key: part[key] for key in ("file_id", "filename", "file_data") if part.get(key) is not None}
            if not fields and isinstance(part.get("file"), dict):
                fields = dict(part["file"])
            parts.append({"kind": "file", "file": fields})
        elif part_type == "input_audio":
            audio = part.get("input_audio") or {k: v for k, v in part.items() if k in ("data", "format")}
            parts.append({"kind": "audio", "audio": audio})
        elif part_type == "refusal":
            parts.append({"kind": "refusal", "text": part.get("refusal", "")})
        else:
            parts.append({"kind": "unknown", "block": part})
    return parts


def parse_responses_input(data: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
    """Responses 请求体 → (instructions 原样, 消息中间条目列表)。

    - input 为字符串时产出单条 user 条目。
    - function_call item → assistant 条目（每个 item 一条，tool_calls 单元素；
      是否把连续 function_call 合并为一条 assistant 消息由渲染端决定）。
    - function_call_output item → tool 条目（output 原样保留）。
    - ``developer`` 角色归一为 ``system``（to_chat 原行为；to_anthropic 原先
      透传 developer 角色会被官方 Anthropic 拒收，收敛后同样归入 system）。
    - 其余 item（含 reasoning / hosted call 等未支持类型）按 role 条目保留，
      ``item_type`` 记录原始类型，丢弃策略由渲染端决定。
    """
    instructions = data.get("instructions")
    entries: list[dict[str, Any]] = []
    input_data = data.get("input", [])
    if isinstance(input_data, str):
        return instructions, [message_entry("user", text=input_data)]
    for item in input_data:
        if isinstance(item, str):
            entries.append(message_entry("user", text=item))
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "")
        if item_type == "function_call":
            entries.append(
                message_entry(
                    "assistant",
                    tool_calls=[
                        {
                            "id": item.get("call_id", item.get("id", "")),
                            "name": item.get("name", ""),
                            "arguments": item.get("arguments", "{}"),
                        }
                    ],
                )
            )
            continue
        if item_type == "function_call_output":
            entries.append(
                message_entry(
                    "tool",
                    tool_results=[
                        {
                            "tool_use_id": item.get("call_id", ""),
                            "content": item.get("output", ""),
                            "is_error": False,
                            "parts": None,
                        }
                    ],
                )
            )
            continue
        role = item.get("role", "user")
        if role == "developer":
            role = "system"
        entry = message_entry(role, item_type=item_type)
        content = item.get("content", "")
        if isinstance(content, str):
            entry["text"] = content
        elif isinstance(content, list):
            entry["parts"] = parse_responses_content_parts(content)
        entries.append(entry)
    return instructions, entries
