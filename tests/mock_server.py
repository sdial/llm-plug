"""Mock upstream API server for testing."""

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

# 请求捕获：保存最近一次请求
_last_chat_request = None

# 各路径命中计数：供 E2E 断言“请求落到了哪个上游路径”（独立进程，经 HTTP 端点读取）
_REQUEST_COUNTS: dict[str, int] = {}


class _RequestCountMiddleware:
    """纯 ASGI 计数中间件（遵循项目约定：不用 BaseHTTPMiddleware，避免流式干扰）"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            _REQUEST_COUNTS[path] = _REQUEST_COUNTS.get(path, 0) + 1
        await self.app(scope, receive, send)


app.add_middleware(_RequestCountMiddleware)


@app.get("/_test/request-counts")
async def request_counts():
    return _REQUEST_COUNTS


@app.post("/_test/reset-counts")
async def reset_request_counts():
    _REQUEST_COUNTS.clear()
    return {"ok": True}


def get_last_chat_request():
    """获取最近一次 /chat/completions 请求体。"""
    return _last_chat_request


def reset_last_chat_request():
    """重置请求捕获。"""
    global _last_chat_request
    _last_chat_request = None


ANTHROPIC_STREAM_DATA = [
    (
        b"event: message_start\n"
        b'data: {"type": "message_start", "message": {"id": "msg_001", "type": "message", '
        b'"role": "assistant", "usage": {"input_tokens": 10, "output_tokens": 0, '
        b'"cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}\n\n'
    ),
    (b'event: content_block_start\ndata: {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}\n\n'),
    (b'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}}\n\n'),
    (b'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " world"}}\n\n'),
    b'event: content_block_stop\ndata: {"type": "content_block_stop", "index": 0}\n\n',
    # 真实 Anthropic Messages 流在 message_stop 前必有 message_delta（含 stop_reason 与 output_tokens）
    (b'event: message_delta\ndata: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}\n\n'),
    b'event: message_stop\ndata: {"type": "message_stop"}\n\n',
]

OPENAI_STREAM_DATA = [
    (
        b'data: {"id": "chatcmpl-001", "object": "chat.completion.chunk", '
        b'"choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": null}]}\n\n'
    ),
    (
        b'data: {"id": "chatcmpl-001", "object": "chat.completion.chunk", '
        b'"choices": [{"index": 0, "delta": {"content": " world"}, "finish_reason": null}]}\n\n'
    ),
    b"data: [DONE]\n\n",
]

# DeepSeek 风格上游：content 内嵌 💭 思考块（跨 chunk），末尾 💭 未闭合，
# 用于验证 ThinkFilter 对 Anthropic 目标格式生效（M2）。
DEEPSEEK_STREAM_DATA = [
    (
        'data: {"id": "chatcmpl-deepseek", "object": "chat.completion.chunk", '
        '"choices": [{"index": 0, "delta": {"content": "💭hidden think"}, "finish_reason": null}]}\n\n'
    ),
    (
        'data: {"id": "chatcmpl-deepseek", "object": "chat.completion.chunk", '
        '"choices": [{"index": 0, "delta": {"content": "💭visible"}, "finish_reason": null}]}\n\n'
    ),
    (
        'data: {"id": "chatcmpl-deepseek", "object": "chat.completion.chunk", '
        '"choices": [{"index": 0, "delta": {"content": "trailing 💭partial"}, "finish_reason": null}]}\n\n'
    ),
]

# DeepSeek 风格上游（发 [DONE] 变体），覆盖 [DONE] 分支的 ThinkFilter flush。
DEEPSEEK_STREAM_DATA_WITH_DONE = DEEPSEEK_STREAM_DATA + [b"data: [DONE]\n\n"]


@app.post("/anthropic/v1/messages")
async def anthropic_messages(request: Request):
    body = await request.json()
    stream = body.get("stream", False)

    if stream:

        async def stream_generator():
            for chunk in ANTHROPIC_STREAM_DATA:
                yield chunk
                await asyncio.sleep(0.01)

        return StreamingResponse(stream_generator(), media_type="text/event-stream")

    return JSONResponse(
        {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello world"}],
            "model": body.get("model", "claude-sonnet-4-20250514"),
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )


@app.post("/openai/v1/chat/completions")
async def openai_chat(request: Request):
    global _last_chat_request
    body = await request.json()
    _last_chat_request = body
    stream = body.get("stream", False)

    if stream:

        async def stream_generator():
            for chunk in OPENAI_STREAM_DATA:
                yield chunk
                await asyncio.sleep(0.01)

        return StreamingResponse(stream_generator(), media_type="text/event-stream")

    return JSONResponse(
        {
            "id": "chatcmpl-001",
            "object": "chat.completion",
            "created": 1234567890,
            "model": body.get("model", "gpt-4o"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello world"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


@app.post("/deepseek/v1/chat/completions")
async def deepseek_chat(request: Request):
    """DeepSeek 风格上游：Chat 格式 + content 内嵌 💭 思考块。"""
    body = await request.json()
    stream = body.get("stream", False)

    if stream:

        async def stream_generator():
            # deepseek-chat-done 模型触发带 [DONE] 的流，覆盖 [DONE] 分支 flush
            stream_data = DEEPSEEK_STREAM_DATA_WITH_DONE if body.get("model") == "deepseek-chat-done" else DEEPSEEK_STREAM_DATA
            for chunk in stream_data:
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                yield chunk
                await asyncio.sleep(0.01)

        return StreamingResponse(stream_generator(), media_type="text/event-stream")

    return JSONResponse(
        {
            "id": "chatcmpl-deepseek",
            "object": "chat.completion",
            "created": 1234567890,
            "model": body.get("model", "deepseek-chat"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello world"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


@app.post("/openai/v1/responses")
async def openai_response(request: Request):
    body = await request.json()
    return JSONResponse(
        {
            "id": "resp_001",
            "object": "response",
            "status": "completed",
            "model": body.get("model", "gpt-4o"),
            "output": [
                {
                    "type": "message",
                    "id": "msg_001",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello world"}],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }
    )


@app.post("/fail-anthropic/v1/messages")
async def failing_anthropic_messages(request: Request):
    """始终 500 的 Anthropic 入口：模拟原生接入点故障（含流式首包前错误）。"""
    return JSONResponse(
        {"type": "error", "error": {"type": "api_error", "message": "upstream down"}},
        status_code=500,
    )


@app.post("/fail-openai/v1/chat/completions")
async def failing_openai_chat(request: Request):
    """始终 500 的 Chat 入口：模拟转格式接入点故障。"""
    return JSONResponse(
        {"error": {"message": "upstream down", "type": "server_error"}},
        status_code=500,
    )


@app.post("/fail-deepseek/v1/chat/completions")
async def fail_deepseek_chat(request: Request):
    """按模型区分行为：deepseek-chat 始终 500（模拟该模型专属故障），
    deepseek-chat-done 正常返回——用于验证 (model, channel) 键隔离：
    A 模型 3 次失败不污染同渠道 B 模型的可选性（A 挂不死 B）。"""
    body = await request.json()
    if body.get("model") == "deepseek-chat":
        return JSONResponse(
            {"error": {"message": "upstream down", "type": "server_error"}},
            status_code=500,
        )
    return await deepseek_chat(request)


@app.post("/429-chat/v1/chat/completions")
async def rate_limited_chat(request: Request):
    """始终 429 的 Chat 入口：模拟瞬时限速（探活 429 分流、预算 0 如实失败）。"""
    return JSONResponse(
        {"error": {"message": "rate limited", "type": "rate_limit_error"}},
        status_code=429,
        headers={"retry-after": "5"},
    )


@app.post("/quota-chat/v1/chat/completions")
async def quota_limited_chat(request: Request):
    """窗口级限速 429（方舟 AccountQuotaExceeded 风格）：驱动 is_blocked 置位。"""
    from datetime import UTC, datetime, timedelta

    reset = (datetime.now(UTC) + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S +0000")
    return JSONResponse(
        {
            "error": {
                "code": "AccountQuotaExceeded",
                "message": f"You have exceeded the usage quota. It will reset at {reset}.",
                "type": "TooManyRequests",
            }
        },
        status_code=429,
    )


@app.post("/404-chat/v1/chat/completions")
async def not_found_chat(request: Request):
    """始终 404 的 Chat 入口：模拟模型不存在（http_4xx_config → permanent 停探）。"""
    return JSONResponse(
        {"error": {"message": "model not found", "type": "invalid_request_error"}},
        status_code=404,
    )


@app.post("/hang-chat/v1/chat/completions")
async def hanging_chat(request: Request):
    """首包永久挂起的流式入口：模拟上游卡死不回数据（探活超时路径）。"""

    async def hang_generator():
        while True:
            await asyncio.sleep(60)
            yield b""

    return StreamingResponse(hang_generator(), media_type="text/event-stream")


async def _echo_anthropic_messages(request: Request, served_by: str):
    """正常 Anthropic 响应体 + 回显捕获到的鉴权/版本头。

    同格式透传不经过 converter，captured_headers / served_by 会原样到客户端，
    供 E2E 断言“实际服务的接入点”与“上游收到的头”。
    """
    body = await request.json()
    return JSONResponse(
        {
            "id": "msg_echo",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello world"}],
            "model": body.get("model", "claude-sonnet-4-20250514"),
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "captured_headers": {
                "x-api-key": request.headers.get("x-api-key"),
                "anthropic-version": request.headers.get("anthropic-version"),
                "anthropic-beta": request.headers.get("anthropic-beta"),
            },
            "served_by": served_by,
        }
    )


@app.post("/echo-anthropic/v1/messages")
async def echo_anthropic_messages(request: Request):
    return await _echo_anthropic_messages(request, "/echo-anthropic/v1/messages")


@app.post("/echo-anthropic-alt/v1/messages")
async def echo_anthropic_alt_messages(request: Request):
    """第二条 echo 路由：供第二个接入点的 url_override 指向。"""
    return await _echo_anthropic_messages(request, "/echo-anthropic-alt/v1/messages")


@app.post("/alt-anthropic/v1/messages")
async def alt_anthropic_messages(request: Request):
    """与 /anthropic/v1/messages 相同形状的正常响应（供第二接入点 url_override 指向）。"""
    return await anthropic_messages(request)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=9999, loop="auto")
