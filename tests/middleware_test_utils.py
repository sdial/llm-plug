"""middleware 包独立单测的合成 ASGI 驱动 helper。"""

import asyncio


def make_scope(
    method: str = "POST",
    path: str = "/v1/chat/completions",
    headers: dict[str, str] | None = None,
    client: tuple = ("127.0.0.1", 12345),
    query_string: bytes = b"",
) -> dict:
    """构造一个最小 http scope。"""
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query_string,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
    }


def make_echo_app(records: dict):
    """返回一个把请求体读全并回 200 的下游 app；records["body"] 记录收到的 body。"""

    async def app(scope, receive, send):
        body_parts = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body_parts.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        records["body"] = b"".join(body_parts)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def run_middleware(middleware, scope, body=b"", chunks=None):
    """驱动中间件，收集发往下游的消息。

    body: 一次投递；chunks: 分批投递（more_body 语义，用于 413 中途超限）。
    返回 (sent_messages, scope)。
    """
    sent = []
    if chunks is not None:
        receive_msgs = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    else:
        receive_msgs = [{"type": "http.request", "body": body, "more_body": False}]

    receive_iter = iter(receive_msgs)

    async def receive():
        try:
            return next(receive_iter)
        except StopIteration:
            return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async def _run():
        await middleware(scope, receive, send)

    asyncio.run(_run())
    return sent, scope
