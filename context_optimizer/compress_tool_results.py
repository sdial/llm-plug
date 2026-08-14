"""阶段一：tool 输出压缩（4 条确定性压缩规则）。

规则都是纯函数（无随机、无时间依赖），同一输入永远同一输出（确定性）；
只修改 tool 结果的文本内容，绝不碰 role / tool_call_id / tool_result 配对，
非文本 content 块（image/audio/file 等）一律跳过（结构完整性）。

注意：规则是确定性而非严格无损——`dedupe_consecutive_lines` 会折叠连续重复
行、`trim_ws` 会去掉行尾空白（破坏 Markdown 行尾两空格换行语义），对依赖
精确输出的下游有隐性风险，两开关均可独立关闭。空行由 `collapse_blank_lines`
统一负责（折叠为两行，保 Markdown 中"区块分隔"的语义），
`dedupe_consecutive_lines` 只去重连续相同的非空行，二者职责不重叠。
"""

from __future__ import annotations

import re

# ANSI/ECMA-48 转义序列，覆盖：
#   1. CSI：ESC [ 参数 中间字节 最终字节（如 \x1b[31m、\x1b[2J、\x1b[?25l）
#   2. CSI 的 8-bit C1 形式（\x9b，非 UTF-8 终端转义）
#   3. OSC/DCS/SOS/PM/APC 控制串：ESC 引入字符 ... BEL(\x07) 或 ESC \ 终止
#   4. 单字节 ESC 序列：ESC 后跟一个最终字节（排除控制串引入字符 P X [ \ ] ^ _）
# Python re 的 alternation 从左到右，控制串分支必须先于单字节分支，
# 否则 ESC ] 会先被单字节分支吃掉导致 OSC 内容残留。
_ANSI_RE = re.compile(
    r"(?:"
    r"\x1b\[[\x30-\x3f]*[ -/]*[@-~]"  # CSI（参数字节 0x30–0x3F：0-9 : ; < = > ?）
    r"|\x9b[\x30-\x3f]*[ -/]*[@-~]"  # CSI (8-bit C1)
    r"|\x1b[PX^_][^\x1b\x07]*(?:\x07|\x1b\\)"  # DCS/SOS/PM/APC 控制串
    r"|\x1b\][^\x1b\x07]*(?:\x07|\x1b\\)"  # OSC 控制串
    r"|\x1b[\x30-\x4f\x51-\x5a\x60-\x7e]"  # 单字节 ESC
    r")"
)
_COLLAPSE_BLANK_RE = re.compile(r"\n{3,}")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _trim_ws(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.split("\n"))


def _collapse_blank_lines(text: str) -> str:
    return _COLLAPSE_BLANK_RE.sub("\n\n\n", text)


def _dedupe_consecutive_lines(text: str) -> str:
    # 空行行（""）不参与去重：空行折叠归 _collapse_blank_lines 负责，
    # 否则两个规则互相打架，collapse 保留的空行会被 dedupe 再次压平。
    lines = text.split("\n")
    out: list[str] = []
    prev: str | None = None
    for line in lines:
        if line and line == prev:
            continue
        out.append(line)
        prev = line
    return "\n".join(out)


def apply_rules(text: str, settings: dict) -> str:
    """按 settings 中的独立开关应用 4 条规则（顺序固定，保证确定性）。"""
    result = text
    if settings.get("ctx_optimize_strip_ansi", True):
        result = _strip_ansi(result)
    if settings.get("ctx_optimize_trim_ws", True):
        result = _trim_ws(result)
    if settings.get("ctx_optimize_collapse_blank_lines", True):
        result = _collapse_blank_lines(result)
    if settings.get("ctx_optimize_dedupe_consecutive_lines", True):
        result = _dedupe_consecutive_lines(result)
    return result


def _compress_in_place(
    container: dict | list,
    key: str | int,
    text: str,
    tool_index: int,
    msg_index: int,
    settings: dict,
    records: list[dict],
) -> None:
    """对 container[key] 处的字符串应用规则；仅当变化时写回并记录。

    tool_index 语义：Chat 为消息下标；Anthropic 为 tool_result 块下标。
    msg_index：消息在 payload["messages"] 中的下标（两种格式一致），
    用于在 JSONL 日志中区分多轮对话里记录来源。
    """
    new_text = apply_rules(text, settings)
    if new_text == text:
        return
    container[key] = new_text
    records.append(
        {
            "tool_index": tool_index,
            "msg_index": msg_index,
            "before": len(text),
            "after": len(new_text),
        }
    )


def _compress_content_list(content: list, tool_index: int, msg_index: int, settings: dict, records: list[dict]) -> None:
    """Chat：role=tool 消息的 content 列表，仅处理 text 块与裸字符串元素。"""
    for idx, item in enumerate(content):
        if isinstance(item, str):
            _compress_in_place(content, idx, item, tool_index, msg_index, settings, records)
        elif isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                _compress_in_place(item, "text", text, tool_index, msg_index, settings, records)


def _compress_tool_result_blocks(blocks: list, msg_index: int, settings: dict, records: list[dict]) -> None:
    """Anthropic：消息 content 中 tool_result 块。块 content 为 str 或 list，
    list 仅处理 text 块与裸字符串。"""
    for block_index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        content = block.get("content")
        if isinstance(content, str):
            _compress_in_place(block, "content", content, block_index, msg_index, settings, records)
        elif isinstance(content, list):
            _compress_content_list(content, block_index, msg_index, settings, records)


def compress_tool_results(payload: dict, settings: dict) -> list[dict]:
    """原地压缩 payload 中的 tool 结果文本。

    返回压缩统计记录列表，每条 {"tool_index", "msg_index", "before", "after"}；
    仅在文本确实发生变化时生成记录。只在 messages 里找：
    - Chat Completions：role == "tool" 消息的 content（str 或 list）
    - Anthropic Messages：任意消息 content 里的 tool_result 块
    其余文本位置（普通 user/assistant/system 文本、Responses 字段）一律不动。

    幂等：对已压缩的 payload 重复调用结果不变（规则是纯函数），
    因此代理在渠道故障转移间复用同一 request_data 对象时原地修改是安全的。
    """
    records: list[dict] = []
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return records
    for msg_index, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if msg.get("role") == "tool":
            if isinstance(content, str):
                _compress_in_place(msg, "content", content, msg_index, msg_index, settings, records)
            elif isinstance(content, list):
                _compress_content_list(content, msg_index, msg_index, settings, records)
        elif isinstance(content, list):
            _compress_tool_result_blocks(content, msg_index, settings, records)
    return records
