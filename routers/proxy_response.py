import json
from collections.abc import AsyncGenerator
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from loguru import logger

from models.api_types import APIType
from proxy_core import ConverterError, proxy_request
from response_state import get_responses_store
from routers.auth import check_proxy_authorization
from routers.proxy_errors import (
    invalid_request,
    response_from_proxy_exception,
    unauthorized,
)

router = APIRouter(tags=["代理"])

_store = get_responses_store()


def _input_to_items(input_data: Any) -> list[dict[str, Any]]:
    if input_data is None:
        return []
    if isinstance(input_data, str):
        return [{"role": "user", "content": input_data}]
    if isinstance(input_data, list):
        items: list[dict[str, Any]] = []
        for item in input_data:
            if isinstance(item, str):
                items.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                items.append(dict(item))
        return items
    return [{"role": "user", "content": str(input_data)}]


def _message_text(item: dict[str, Any]) -> str:
    text_parts: list[str] = []
    for content in item.get("content", []):
        if not isinstance(content, dict):
            continue
        if content.get("type") in ("output_text", "input_text"):
            text_parts.append(content.get("text", ""))
    return "\n".join(part for part in text_parts if part)


def _response_output_to_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in response.get("output", []):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            items.append({
                "role": item.get("role", "assistant"),
                "content": _message_text(item),
            })
        elif item_type == "function_call":
            items.append({
                "type": "function_call",
                "call_id": item.get("call_id", item.get("id", "")),
                "name": item.get("name", ""),
                "arguments": item.get("arguments", "{}"),
            })
        elif item_type == "reasoning":
            # reasoning 项保留在历史中，供 debug 和未来回放使用
            # Chat 上游不支持 reasoning 回放，但保存原样
            items.append({
                "type": "reasoning",
                "id": item.get("id", ""),
                "summary": item.get("summary", []),
            })
    return items


async def _save_response_state(
    request_body: dict[str, Any],
    previous_conversation: dict[str, Any] | None,
    response: dict[str, Any],
) -> None:
    response_id = response.get("id")
    if not response_id:
        # 上游未返回 id 时不再本地伪造 —— 同格式透传场景下，本地伪造的 id 上游不认识，
        # 客户端下次用它当 previous_response_id 会触发上游 404，链路静默断裂。
        logger.warning("[RESPONSES] upstream response missing 'id', skipping local state store")
        return

    messages = list((previous_conversation or {}).get("messages", []))
    messages.extend(_input_to_items(request_body.get("input")))
    messages.extend(_response_output_to_items(response))

    conversation = {
        "messages": messages,
        "instructions": request_body.get("instructions") or (previous_conversation or {}).get("instructions", ""),
        "reasoning_history": [],
        "tool_calls": [],
    }
    await _store.put(response_id, conversation, response)


def _extract_response_completed(sse_text: str) -> dict[str, Any] | None:
    for block in sse_text.split("\n\n"):
        data_lines = []
        for line in block.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            continue
        data_str = "\n".join(data_lines).strip()
        if not data_str or data_str == "[DONE]":
            continue
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "response.completed" and isinstance(data.get("response"), dict):
            return data["response"]
    return None


async def _stateful_stream(
    gen: AsyncGenerator,
    request_body: dict[str, Any],
    previous_conversation: dict[str, Any] | None,
    should_store: bool,
):
    completed_response: dict[str, Any] | None = None
    buffer = ""
    try:
        async for chunk in gen:
            text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            buffer += text
            if "\n\n" in buffer:
                complete, buffer = buffer.rsplit("\n\n", 1)
                parsed = _extract_response_completed(complete + "\n\n")
                if parsed is not None:
                    completed_response = parsed
            yield chunk
    finally:
        if buffer:
            parsed = _extract_response_completed(buffer)
            if parsed is not None:
                completed_response = parsed
        try:
            await gen.aclose()
        finally:
            if should_store and completed_response is not None:
                try:
                    await _save_response_state(request_body, previous_conversation, completed_response)
                except Exception as e:
                    logger.warning(f"Failed to save streamed response state: {e}")


@router.post("/v1/responses")
async def post_response(request: Request, authorization: Annotated[str | None, Header()] = None):
    if not check_proxy_authorization(authorization, request.state):
        return unauthorized()

    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        return invalid_request(f"Invalid JSON: {e}")

    model = body.get("model", "")
    is_stream = body.get("stream", False)
    query_string = str(request.url.query) if request.url.query else None
    client_headers = dict(request.headers)
    api_key_id = getattr(request.state, "api_key_id", None)
    client_ip = request.client.host if request.client else None

    logger.debug(f"[RESPONSES REQUEST] model={model} stream={is_stream} api_key_id={api_key_id}")

    try:
        result, channel = await proxy_request(
            model, body, APIType.OPENAI_RESPONSE, is_stream,
            query_string=query_string, client_headers=client_headers,
            api_key_id=api_key_id,
            client_ip=client_ip,
        )
        request.state.selected_channel_name = channel.name
        logger.debug(f"[RESPONSES SUCCESS] model={model} channel={channel.name}")
    except ValueError as e:
        logger.error(f"[RESPONSES ERROR] /v1/responses ValueError: {e}")
        return invalid_request(str(e))
    except ConverterError as e:
        logger.error(f"[RESPONSES ERROR] /v1/responses ConverterError: {e}")
        return invalid_request(str(e))
    except httpx.HTTPStatusError as e:
        logger.error(f"[RESPONSES ERROR] /v1/responses upstream HTTP {e.response.status_code}: {e}")
        return _response_from_upstream_http_error(e)
    except Exception as e:
        logger.error(f"[RESPONSES ERROR] /v1/responses {type(e).__name__}: {e}")
        return response_from_proxy_exception(e)

    should_store = body.get("store", True) is not False
    previous_conversation = None
    if should_store and body.get("previous_response_id"):
        previous_conversation = await _store.get_conversation(body["previous_response_id"])
    if is_stream:
        return StreamingResponse(
            _stateful_stream(result, body, previous_conversation, should_store),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    if should_store and isinstance(result, dict):
        await _save_response_state(body, previous_conversation, result)

    return JSONResponse(content=result)


@router.get("/v1/responses/{response_id}")
async def get_response(response_id: str):
    response = await _store.get_response(response_id)
    if response is None:
        raise HTTPException(status_code=404, detail=f"Response {response_id} not found")
    return response


@router.delete("/v1/responses/{response_id}")
async def delete_response(response_id: str):
    deleted = await _store.delete(response_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Response {response_id} not found")
    return {"deleted": True, "id": response_id}


def _response_from_upstream_http_error(exc: httpx.HTTPStatusError) -> Response:
    try:
        content = exc.response.content
    except httpx.ResponseNotRead:
        content = exc.response.read()
    media_type = exc.response.headers.get("content-type")
    return Response(
        content=content,
        status_code=exc.response.status_code,
        media_type=media_type,
    )
