"""Mock upstream API server for testing."""
import asyncio
import json
from typing import AsyncGenerator
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

app = FastAPI()

ANTHROPIC_STREAM_DATA = [
    b'event: message_start\ndata: {"type": "message_start", "message": {"id": "msg_001", "type": "message", "role": "assistant"}}\n\n',
    b'event: content_block_start\ndata: {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}\n\n',
    b'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}}\n\n',
    b'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " world"}}\n\n',
    b'event: content_block_stop\ndata: {"type": "content_block_stop", "index": 0}\n\n',
    b'event: message_stop\ndata: {"type": "message_stop"}\n\n',
]

OPENAI_STREAM_DATA = [
    b'data: {"id": "chatcmpl-001", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": null}]}\n\n',
    b'data: {"id": "chatcmpl-001", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": " world"}, "finish_reason": null}]}\n\n',
    b'data: [DONE]\n\n',
]


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
    
    return JSONResponse({
        "id": "msg_001",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Hello world"}],
        "model": body.get("model", "claude-sonnet-4-20250514"),
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5}
    })


@app.post("/openai/v1/chat/completions")
async def openai_chat(request: Request):
    body = await request.json()
    stream = body.get("stream", False)
    
    if stream:
        async def stream_generator():
            for chunk in OPENAI_STREAM_DATA:
                yield chunk
                await asyncio.sleep(0.01)
        return StreamingResponse(stream_generator(), media_type="text/event-stream")
    
    return JSONResponse({
        "id": "chatcmpl-001",
        "object": "chat.completion",
        "created": 1234567890,
        "model": body.get("model", "gpt-4o"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Hello world"},
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    })


@app.post("/openai/v1/responses")
async def openai_response(request: Request):
    body = await request.json()
    return JSONResponse({
        "id": "resp_001",
        "object": "response",
        "status": "completed",
        "model": body.get("model", "gpt-4o"),
        "output": [{
            "type": "message",
            "id": "msg_001",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Hello world"}]
        }],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9999)
