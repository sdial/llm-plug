"""敏感信息拦截异常。

单独文件避免 proxy.errors 与 pii_filter 之间的循环导入。
"""


class SensitiveBlockError(Exception):
    """请求包含被 block 规则命中的敏感信息，禁止发往上游。"""

    def __init__(self, message: str, triggered: list[str] | None = None):
        super().__init__(message)
        self.triggered = triggered or []
