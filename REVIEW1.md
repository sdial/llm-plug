## 🟠 Important（影响正确性或性能，应尽快修）

**安全 / 持久化**
- `[sec]` `stats.py:783-787` & `request_logs.py:236-241` **LIKE 通配字符未转义**，输入 `%` 等价匹配一切
- `[sec]` `get_request_field` **字段名拼 SQL**（虽有白名单但脆弱模式） `request_logs.py:491-507`
- `[sec]` `request_body`/`response_body` **无体积上限**，长期会爆盘
- `[sec]` `request_logs`/`stats` 队列满直接 `put_nowait` **丢日志/审计**
- `[sec]` `storage.py:85-99` `load_data` 5s 缓存 + admin 立即写回，**lost-update 竞态**
- `[sec]` `routers/admin.py:609-627 PUT /admin/settings` 用 `dict` 接收 body，**无 pydantic 校验**，可写入 `max_body_size=0` 等危险值

**架构 / 并发**  CODEX
- `[arch]` `CombinedMiddleware` 把请求体全 buffer 进内存，**未读 Content-Length 提前拒绝**
- `[arch]` `tracking_send` 默认 `response_status=200`，**异常路径误报 200**
- `[arch]` 每个请求都 `load_api_keys()` + **O(N) 线性匹配**
- `[arch]` `_is_channel_config_error` 把 401/403 当配置错，**一次错配会拉黑所有渠道**
- `[arch]` 流式中途出错后 emit error 事件，**缺 Anthropic `message_stop` 或 SSE `[DONE]`**

**转换器**
- `[conv]` `merge_system_messages` 对 list 类型 system content 会 **TypeError**
- `[conv]` `_filter_content_type` 仅浅拷贝，**污染调用方 messages**，多渠道 fallback 会传递被改过的请求
- `[conv]` `to_chat.py` 与 `to_response.py` 的 **thinking budget→reasoning_effort 阈值不一致**
- `[conv]` Anthropic→Response：tool_use + text 同一 message 时**输出顺序错乱**（function_call 跑到 text 前）
- `[conv]` `think_filter.py` 短 chunk 持续累积 buffer，**首批文本延迟到流末才出现**
- `[conv]` `total_tokens` 自己累加，**忽略 cached/reasoning tokens**，计费偏低

---

## 🟡 Minor（健壮性 / 可读性）

- `[arch]` `state_store.get_response` 每次读都写盘更新 last_access_at（IO 放大）
- `[arch]` `_schedule_invalidate_model_channels_cache` fire-and-forget task **未保留强引用**（Python 3.11+ 警告）
- `[arch]` `proxy_core.py:339` `chatcmpl-{model[:8]}` 兜底 ID **不唯一**
- `[sec]` `serve_viewer.py` `Access-Control-Allow-Origin: *` 且无认证
- `[sec]` `stats.py:32-34` `datetime.utcnow()` 已弃用，落库 naive 时间无 tz
- `[conv]` `to_anthropic.py:115-120` `_convert_content` 分支顺序错误，list 中纯字符串元素被静默丢弃
- `[conv]` `to_response.py:794-795` `response.in_progress.response.model` 写死空字符串
- `[conv]` `to_response.py:917` `function_call_arguments.done` 缺 `name` 字段
- `[conv]` `_tool_result_to_chat_message` 把 image content 静默吞掉
