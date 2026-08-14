"""Claude Code 遥测字段归一化。

只处理 Anthropic Messages 请求中的动态遥测信息（日期、cwd、OS、模型元数据等），
通过确定性正则把动态值替换为固定占位符，使同一项目/会话内的 prompt 前缀稳定，
从而提升 Anthropic prompt cache 命中率。

规则均为纯函数（无随机、无时间依赖），同一输入永远同一输出；
只修改文本内容，绝不碰消息结构、role、tool_call_id、tool_use/tool_result 配对。
"""

from __future__ import annotations

import re

# 每条规则都是 (compiled_regex, replacement)，按固定顺序应用。
# 全部使用 MULTILINE，让 ^/$ 匹配行首行尾，避免跨行误伤。
_TELEMETRY_RULES: list[tuple[re.Pattern[str], str]] = [
    # 简单模式：CWD / Date
    (re.compile(r"^CWD: .*$", re.MULTILINE), "CWD: <cwd>"),
    (re.compile(r"^Date: \d{4}-\d{2}-\d{2}$", re.MULTILINE), "Date: <date>"),
    # 完整 env 块中的 bullet 项
    (re.compile(r"^ - Primary working directory: .*$", re.MULTILINE), " - Primary working directory: <cwd>"),
    (re.compile(r"^ - Is a git repository: .*$", re.MULTILINE), " - Is a git repository: <git_repo>"),
    (re.compile(r"^ - Platform: .*$", re.MULTILINE), " - Platform: <platform>"),
    (re.compile(r"^ - Shell: .*$", re.MULTILINE), " - Shell: <shell>"),
    (re.compile(r"^ - OS Version: .*$", re.MULTILINE), " - OS Version: <os_version>"),
    (
        re.compile(
            r"^ - You are powered by the model named .+\. The exact model ID is .+\.$",
            re.MULTILINE,
        ),
        " - You are powered by the model named <model>. The exact model ID is <model_id>.",
    ),
    (
        re.compile(r"^ - Assistant knowledge cutoff is .+\.$", re.MULTILINE),
        " - Assistant knowledge cutoff is <cutoff>.",
    ),
    (
        re.compile(r"^ - The most recent Claude model family is .+$", re.MULTILINE),
        " - The most recent Claude model family is <model_family>.",
    ),
    # XML/env 块中的平铺项（也覆盖简单 env 块）
    (re.compile(r"^Working directory: .*$", re.MULTILINE), "Working directory: <cwd>"),
    (
        re.compile(r"^Is directory a git repo: .*$", re.MULTILINE),
        "Is directory a git repo: <git_repo>",
    ),
    (re.compile(r"^Platform: .*$", re.MULTILINE), "Platform: <platform>"),
    (re.compile(r"^Shell: .*$", re.MULTILINE), "Shell: <shell>"),
    (re.compile(r"^OS Version: .*$", re.MULTILINE), "OS Version: <os_version>"),
    (re.compile(r"^Today's date: \d{4}-\d{2}-\d{2}$", re.MULTILINE), "Today's date: <date>"),
    # system-reminder 中的 currentDate
    (
        re.compile(r"^Today's date is \d{4}-\d{2}-\d{2}\.$", re.MULTILINE),
        "Today's date is <date>.",
    ),
    # 会话中日期刷新提示
    (
        re.compile(r"The date has changed\. Today's date is now \d{4}-\d{2}-\d{2}\."),
        "The date has changed. Today's date is now <date>.",
    ),
]


def _strip_telemetry_text(text: str) -> str:
    """对单段文本应用全部 telemetry 归一化规则。"""
    result = text
    for pattern, replacement in _TELEMETRY_RULES:
        result = pattern.sub(replacement, result)
    return result


def _strip_text_in_place(
    container: dict | list,
    key: str | int,
    text: str,
    location: str,
    records: list[dict],
) -> None:
    """对 container[key] 处的字符串应用遥测归一化；仅当变化时写回并记录。"""
    new_text = _strip_telemetry_text(text)
    if new_text == text:
        return
    container[key] = new_text
    records.append({"location": location, "before": len(text), "after": len(new_text)})


def _strip_content_list(content: list, location_prefix: str, records: list[dict]) -> None:
    """处理 content 列表中的 text 块与裸字符串元素。"""
    for idx, item in enumerate(content):
        if isinstance(item, str):
            _strip_text_in_place(content, idx, item, f"{location_prefix}.{idx}", records)
        elif isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                _strip_text_in_place(item, "text", text, f"{location_prefix}.{idx}", records)


def _strip_system_field(payload: dict, records: list[dict]) -> None:
    """处理 Anthropic 顶层 system 字段（字符串或 text block 列表）。"""
    system = payload.get("system")
    if isinstance(system, str):
        _strip_text_in_place(payload, "system", system, "system", records)
        return
    if not isinstance(system, list):
        return
    for idx, block in enumerate(system):
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                _strip_text_in_place(block, "text", text, f"system.{idx}", records)
        elif isinstance(block, str):
            _strip_text_in_place(system, idx, block, f"system.{idx}", records)


def _strip_tool_result_content(block: dict, location_prefix: str, records: list[dict]) -> None:
    """处理单个 tool_result 的 content（str 或 list）。"""
    content = block.get("content")
    if isinstance(content, str):
        _strip_text_in_place(block, "content", content, location_prefix, records)
    elif isinstance(content, list):
        _strip_content_list(content, location_prefix, records)


def _strip_messages(messages: list, records: list[dict]) -> None:
    """遍历 messages，处理 Anthropic tool_result 块中的文本。"""
    if not isinstance(messages, list):
        return
    for _, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for block_index, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    _strip_tool_result_content(block, f"tool_result.{block_index}", records)


def strip_claude_code_telemetry(payload: dict, settings: dict) -> list[dict]:
    """原地归一化 payload 中的 Claude Code 遥测字段。

    返回变更记录列表，每条 {"location", "before", "after"}；
    仅在文本确实发生变化时生成记录。

    处理范围：
    - Anthropic 顶层 system 字段（str 或 text block 列表）
    - messages 中 Anthropic tool_result 块的字符串/list content

    未启用 / 无变化时零开销返回空列表。
    """
    records: list[dict] = []
    if not settings.get("ctx_optimize_strip_cc_telemetry"):
        return records
    _strip_system_field(payload, records)
    _strip_messages(payload.get("messages"), records)
    return records
