import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from loguru import logger

import config
import quota_limits
from balancer.load_balancer import load_balancer
from channel_catalog import catalog
from client import create_client, get_upstream_headers
from models.api_types import APIType
from models.channel import Channel
from proxy import outcomes
from proxy.dispatcher import DispatchContext
from proxy.dispatcher import dispatch_pinned as _dispatch_pinned
from proxy.endpoint_execution import _extract_response_body
from proxy.errors import (
    AllChannelsExhausted,
    ConverterError,
    classify_failure,
)
from proxy.outcomes import OutcomeKind
from proxy.routing import proxy_request
from rate_limiting import (
    RateLimitExceeded,
    acquire_send_budget,
)
from response_state import get_responses_store
from routers.auth import check_proxy_authorization
from routers.proxy_errors import (
    invalid_request,
    response_from_proxy_exception,
    safe_httpx_response_content,
    unauthorized,
)
from url_builder import append_api_path, append_query

router = APIRouter(tags=["代理"])

_store = get_responses_store()

_SKIP_FORWARD_HEADERS = {
    "host",
    "authorization",
    "x-api-key",
    "content-type",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _json_response_from_upstream(resp: httpx.Response) -> Response:
    media_type = resp.headers.get("content-type")
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=media_type,
    )


def _forward_headers(channel: Channel, endpoint, client_headers: dict[str, str] | None, has_body: bool) -> dict[str, str]:
    forwarded = {key: value for key, value in (client_headers or {}).items() if key.lower() not in _SKIP_FORWARD_HEADERS}
    headers = get_upstream_headers(channel, forwarded, endpoint=endpoint)
    if has_body:
        headers["Content-Type"] = "application/json"
    return headers


async def _select_responses_channel(
    *,
    model: str | None = None,
    exclude_ids: set[str] | None = None,
    client_ip: str | None = None,
    api_key_id: str | None = None,
    client_headers: dict[str, str] | None = None,
) -> tuple[Channel, Any]:
    snapshot = await catalog.snapshot()
    channels: list[Channel] = []
    endpoints: dict[str, Any] = {}
    for channel in snapshot.channels:
        # 透传扩展端点只服务原生 Responses 接入点（ADR-0006）
        responses_ep = channel.enabled_endpoint_for(APIType.OPENAI_RESPONSE)
        if responses_ep is None or not channel.enabled:
            continue
        if model and model not in channel.models:
            continue
        channels.append(channel)
        endpoints[channel.id] = responses_ep

    if not channels:
        if model:
            raise ValueError(f"No available OpenAI Responses channel supports model: {model}")
        raise ValueError("No OpenAI Responses channel is available")

    selected = await load_balancer.select_channel(
        channels,
        exclude_ids=exclude_ids,
        model=model,
        client_ip=client_ip,
        api_key_id=api_key_id,
        client_headers=client_headers,
    )
    if selected is None:
        raise ValueError("No healthy OpenAI Responses channel is available")
    return selected, endpoints[selected.id]


async def _read_json_body(request: Request) -> dict[str, Any]:
    body_bytes = await request.body()
    if not body_bytes:
        return {}
    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")
    return body


async def _forward_responses_request(
    request: Request,
    path: str,
    *,
    method: str,
    model: str | None = None,
    body_loader: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> Response:
    """透传扩展端点（GET/DELETE/POST）到上游原生 Responses 渠道。

    选路 / 429 预算 / 窗口限速快速失败 / 排除集收敛至 :mod:`proxy.dispatcher`
    （ADR-0011）；「预算内重试同一渠道」经 :func:`proxy.dispatcher.dispatch_pinned`
    一等入口表达（ADR-0021 D2，标准准入档）。失败 / 成功记账在 ``attempt_fn`` 内经
    :func:`proxy.errors.classify_failure` 裸状态码判定 kind（ADR-0014 D0）并返回
    透传 ``Response``，不重选渠道（透传语义）。
    """
    client_headers = dict(request.headers)
    api_key_id = getattr(request.state, "api_key_id", None)
    client_ip = getattr(request.state, "client_ip", None)
    body: dict[str, Any] | None = None
    if body_loader is not None:
        body = await body_loader()
        if model is None:
            model = body.get("model")

    channel, endpoint = await _select_responses_channel(
        model=model,
        client_ip=client_ip,
        api_key_id=api_key_id,
        client_headers=client_headers,
    )
    request.state.selected_channel_name = channel.name
    url = append_api_path(endpoint.base_url, path)
    url = append_query(url, str(request.url.query) if request.url.query else None)
    headers = _forward_headers(channel, endpoint, client_headers, body is not None)

    # 健康键 model 段推导唯一住所 outcomes.effective_model（ADR-0025 D0）：
    # body-model 已于上方提取进 model，不重复传 body_model
    health_model = outcomes.effective_model(model=model, channel_id=channel.id)
    # 与 proxy/routing.py 一致的限速等待预算：429 重试与发送前排队共用，单请求内共享
    ctx = DispatchContext(
        wait_budget=float(config.get_setting("rate_limit_wait_seconds") or 0),
    )

    async def attempt_fn(selected: Channel, wait_budget: float) -> tuple[Response, Channel]:
        # 发送前排队超时抛 RateLimitExceeded，由 dispatcher 记 rate_limit_exhausted
        # 后转故障转移，外层再映射为 JSON 429
        await acquire_send_budget(selected, wait_budget)
        client = await create_client(selected)
        resp = await client.request(method, url, json=body, headers=headers)
        # 部分测试/上游客户端不回填 response.request，兜底构造（仅作异常载体）
        request_obj = getattr(resp, "_request", None) or httpx.Request(method, url)
        if resp.status_code == 429:
            info = quota_limits.detect_body(_extract_response_body(resp))
            if info is not None:
                # 窗口级限速：直接抛 HTTPStatusError，dispatcher 的
                # _handle_rate_limit_or_fast_fail 会记账 + 原样重抛，
                # 外层透传上游原始 429
                raise httpx.HTTPStatusError("quota window", request=request_obj, response=resp)
            # 瞬时限速：抛 HTTPStatusError，dispatcher 的 handle_rate_limit 扣预算重试
            raise httpx.HTTPStatusError("rate limited", request=request_obj, response=resp)
        # kind 判定走 classify_failure 裸状态码路径（ADR-0014 D0，无异常对象场景）：
        # 命中明确失败档（5xx / 401-403-404）按状态记账失败；兜底档（2xx / 400 等
        # 歧义 4xx）记成功——透传不因歧义状态惩罚渠道。记账不重选（透传语义，ADR-0011）。
        kind = classify_failure(status=resp.status_code)
        if kind is OutcomeKind.transport_failure:
            outcomes.record(health_model, selected.id, OutcomeKind.success)
        else:
            outcomes.record(health_model, selected.id, kind)
        return _json_response_from_upstream(resp), selected

    try:
        resp, _ = await _dispatch_pinned(
            channel,
            attempt_fn,
            model=health_model,
            context=ctx,
            admission=True,
            client_ip=client_ip,
            api_key_id=api_key_id,
            client_headers=client_headers,
        )
        return resp
    except AllChannelsExhausted as exhausted:
        last_error = exhausted.last_error
        if isinstance(last_error, RateLimitExceeded) and getattr(last_error, "queue_timeout", False):
            logger.warning(f"[RATE LIMIT] responses 端点发送排队超时 url={url} waited={last_error.waited:.1f}s")
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": str(last_error),
                        "type": "rate_limit_error",
                        "code": "rate_limit_exceeded",
                    }
                },
            )
        if isinstance(last_error, httpx.HTTPStatusError):
            return _json_response_from_upstream(last_error.response)
        raise


async def _handle_forward_error(exc: BaseException) -> Response:
    if isinstance(exc, ValueError):
        return invalid_request(str(exc))
    if isinstance(exc, httpx.HTTPStatusError):
        return _response_from_upstream_http_error(exc)
    if isinstance(exc, httpx.TimeoutException):
        return response_from_proxy_exception(exc)
    if isinstance(exc, httpx.RequestError):
        return response_from_proxy_exception(exc)
    return response_from_proxy_exception(exc)


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
            items.append(
                {
                    "role": item.get("role", "assistant"),
                    "content": _message_text(item),
                }
            )
        elif item_type == "function_call":
            items.append(
                {
                    "type": "function_call",
                    "call_id": item.get("call_id", item.get("id", "")),
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "{}"),
                }
            )
        elif item_type == "reasoning":
            # reasoning 项保留在历史中，供 debug 和未来回放使用
            # Chat 上游不支持 reasoning 回放，但保存原样
            items.append(
                {
                    "type": "reasoning",
                    "id": item.get("id", ""),
                    "summary": item.get("summary", []),
                }
            )
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
            try:
                await asyncio.wait_for(gen.aclose(), timeout=2.0)
            except TimeoutError:
                logger.warning("[STATEFUL STREAM ACLOSE TIMEOUT] gen.aclose() timeout 2.0s")
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception as e:
                logger.warning(f"stateful stream aclose error: {e}")
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
    client_ip = getattr(request.state, "client_ip", None)

    logger.debug(f"[RESPONSES REQUEST] model={model} stream={is_stream} api_key_id={api_key_id}")

    try:
        result, channel = await proxy_request(
            model,
            body,
            APIType.OPENAI_RESPONSE,
            is_stream,
            query_string=query_string,
            client_headers=client_headers,
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
    except AllChannelsExhausted as e:
        logger.error(f"[RESPONSES ERROR] /v1/responses AllChannelsExhausted: {e}")
        if isinstance(e.last_error, httpx.HTTPStatusError):
            return _response_from_upstream_http_error(e.last_error)
        return response_from_proxy_exception(e.last_error or e)
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


@router.post("/v1/responses/input_tokens")
async def count_response_input_tokens(request: Request, authorization: Annotated[str | None, Header()] = None):
    if not check_proxy_authorization(authorization, request.state):
        return unauthorized()
    try:
        return await _forward_responses_request(
            request,
            "/responses/input_tokens",
            method="POST",
            body_loader=lambda: _read_json_body(request),
        )
    except Exception as exc:
        logger.error(f"[RESPONSES ERROR] /v1/responses/input_tokens {type(exc).__name__}: {exc}")
        return await _handle_forward_error(exc)


@router.post("/v1/responses/compact")
async def compact_response(request: Request, authorization: Annotated[str | None, Header()] = None):
    if not check_proxy_authorization(authorization, request.state):
        return unauthorized()
    try:
        return await _forward_responses_request(
            request,
            "/responses/compact",
            method="POST",
            body_loader=lambda: _read_json_body(request),
        )
    except Exception as exc:
        logger.error(f"[RESPONSES ERROR] /v1/responses/compact {type(exc).__name__}: {exc}")
        return await _handle_forward_error(exc)


@router.get("/v1/responses/{response_id}/input_items")
async def list_response_input_items(
    response_id: str,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
):
    if not check_proxy_authorization(authorization, request.state):
        return unauthorized()
    try:
        return await _forward_responses_request(
            request,
            f"/responses/{response_id}/input_items",
            method="GET",
        )
    except Exception as exc:
        logger.error(f"[RESPONSES ERROR] /v1/responses/{response_id}/input_items {type(exc).__name__}: {exc}")
        return await _handle_forward_error(exc)


@router.post("/v1/responses/{response_id}/cancel")
async def cancel_response(
    response_id: str,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
):
    if not check_proxy_authorization(authorization, request.state):
        return unauthorized()
    try:
        return await _forward_responses_request(
            request,
            f"/responses/{response_id}/cancel",
            method="POST",
        )
    except Exception as exc:
        logger.error(f"[RESPONSES ERROR] /v1/responses/{response_id}/cancel {type(exc).__name__}: {exc}")
        return await _handle_forward_error(exc)


@router.get("/v1/responses/{response_id}")
async def get_response(
    response_id: str,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
):
    if not check_proxy_authorization(authorization, request.state):
        return unauthorized()
    try:
        return await _forward_responses_request(
            request,
            f"/responses/{response_id}",
            method="GET",
        )
    except Exception as exc:
        logger.error(f"[RESPONSES ERROR] /v1/responses/{response_id} {type(exc).__name__}: {exc}")
        return await _handle_forward_error(exc)


@router.delete("/v1/responses/{response_id}")
async def delete_response(
    response_id: str,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
):
    if not check_proxy_authorization(authorization, request.state):
        return unauthorized()
    try:
        return await _forward_responses_request(
            request,
            f"/responses/{response_id}",
            method="DELETE",
        )
    except Exception as exc:
        logger.error(f"[RESPONSES ERROR] DELETE /v1/responses/{response_id} {type(exc).__name__}: {exc}")
        return await _handle_forward_error(exc)


def _response_from_upstream_http_error(exc: httpx.HTTPStatusError) -> Response:
    content = safe_httpx_response_content(exc.response)
    if content is None:
        return JSONResponse(
            status_code=exc.response.status_code,
            content={
                "error": {
                    "message": f"Upstream HTTP {exc.response.status_code}: {exc}",
                    "type": "api_error",
                }
            },
        )
    media_type = exc.response.headers.get("content-type")
    return Response(
        content=content,
        status_code=exc.response.status_code,
        media_type=media_type,
    )
