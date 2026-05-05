import secrets
from typing import Any


def parse_responses_request(body: dict[str, Any]) -> dict[str, Any]:
    """解析 Responses API 请求"""
    if not body.get("model"):
        raise ValueError("'model' is required")
    if "input" not in body:
        raise ValueError("'input' is required")

    return {
        "model": body["model"],
        "input": body["input"],
        "instructions": body.get("instructions", ""),
        "tools": body.get("tools", []),
        "tool_choice": body.get("tool_choice", "auto"),
        "parallel_tool_calls": body.get("parallel_tool_calls", True),
        "reasoning": body.get("reasoning"),
        "stream": body.get("stream", False),
        "previous_response_id": body.get("previous_response_id"),
        "store": body.get("store", True),
    }


def build_input_messages(input_data: str | list[dict]) -> list[dict[str, str]]:
    """将 input 转换为 ChatMessage 列表"""
    messages = []

    if isinstance(input_data, str):
        messages.append({"role": "user", "content": input_data})
        return messages

    for item in input_data:
        role = item.get("role", "user")
        # developer → system 规范化
        if role == "developer":
            role = "system"
        messages.append({"role": role, "content": item.get("content", "")})

    return messages


def generate_response_id() -> str:
    """生成 response_id: resp_ + 24字符hex"""
    return f"resp_{secrets.token_hex(12)}"


def build_chat_request(
    model: str,
    instructions: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    tool_choice: str | dict = "auto",
    stream: bool = False,
) -> dict[str, Any]:
    """构建 Chat API 请求"""
    all_messages = []

    # instructions → system message
    if instructions:
        all_messages.append({"role": "system", "content": instructions})

    all_messages.extend(messages)

    result = {
        "model": model,
        "messages": all_messages,
        "stream": stream,
    }

    if tools:
        result["tools"] = _convert_tools_to_chat_format(tools)

    if tool_choice:
        result["tool_choice"] = tool_choice

    return result


def _convert_tools_to_chat_format(tools: list[dict]) -> list[dict]:
    """将 Responses tools 格式转换为 Chat tools 格式"""
    chat_tools = []
    for tool in tools:
        if tool.get("type") == "function":
            chat_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {}),
                    "strict": tool.get("strict", False),
                },
            })
    return chat_tools


def build_responses_output(
    response_id: str,
    model: str,
    assistant_content: str,
    usage: dict | None = None,
    tool_calls: list[dict] | None = None,
) -> dict[str, Any]:
    """构建 Responses API 响应"""
    import time

    output = []

    if assistant_content:
        output.append({
            "type": "message",
            "id": f"msg_{response_id}",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": assistant_content}],
        })

    if tool_calls:
        for tc in tool_calls:
            output.append({
                "type": "function_call",
                "call_id": tc.get("id", ""),
                "name": tc.get("function", {}).get("name", ""),
                "arguments": tc.get("function", {}).get("arguments", "{}"),
            })

    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "status": "completed",
        "output": output,
        "output_text": assistant_content or "",
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
