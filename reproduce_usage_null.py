"""复现 to_anthropic 流式 chunk 中 usage=null 导致的转换失败。"""

import sys

sys.path.insert(0, r"H:\temp\temp\llm-plug")

from converters.to_anthropic import ToAnthropicConverter
from models.api_types import APIType


def main():
    converter = ToAnthropicConverter()
    chunk = {
        "id": "chatcmpl-test",
        "model": "z-ai/glm-5.2",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant"},
                "finish_reason": None,
            }
        ],
        "usage": None,  # NVIDIA 等上游可能返回 usage: null
    }
    try:
        result = converter.convert_stream_chunk(chunk, APIType.OPENAI_CHAT.value)
        print("转换成功:", result)
    except Exception as exc:
        print(f"转换失败: {type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    main()
