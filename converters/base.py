from abc import ABC, abstractmethod
from typing import Any


class BaseConverter(ABC):
    """转换器基类，定义格式转换接口。

    `source_type` 由 proxy_core 传入，为上游渠道 `Channel.api_type` 的字符串值
    （如 ``openai-chat-completions``），多源转换实现可按需分支。
    """

    @abstractmethod
    def convert_request(
        self, source_data: dict[str, Any], source_type: str = ""
    ) -> dict[str, Any]:
        """将入口请求体转为上游 API 所需 JSON。"""
        pass

    @abstractmethod
    def convert_response(
        self, target_response: dict[str, Any], source_type: str = ""
    ) -> dict[str, Any]:
        """将上游非流式 JSON 转为入口 API 对应格式。"""
        pass

    @abstractmethod
    def convert_stream_chunk(
        self, chunk: dict[str, Any], source_type: str = ""
    ) -> dict[str, Any] | None:
        """将上游 SSE 解析出的单条 JSON 转为入口格式；返回 None 表示跳过该块。"""
        pass
