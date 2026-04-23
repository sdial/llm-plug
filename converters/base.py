from abc import ABC, abstractmethod
from typing import Any


class BaseConverter(ABC):
    """转换器基类，定义格式转换接口"""

    @abstractmethod
    def convert_request(self, source_data: dict[str, Any]) -> dict[str, Any]:
        """将源格式请求转换为目标格式"""
        pass

    @abstractmethod
    def convert_response(self, target_response: dict[str, Any]) -> dict[str, Any]:
        """将目标格式响应转换回源格式"""
        pass

    @abstractmethod
    def convert_stream_chunk(self, chunk: dict[str, Any]) -> dict[str, Any] | None:
        """将流式响应块转换回源格式，返回None表示跳过"""
        pass
