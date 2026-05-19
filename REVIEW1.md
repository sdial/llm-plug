## 🟠 Important（影响正确性或性能，应尽快修）


**转换器**
- ~~`[conv]` `merge_system_messages` 对 list 类型 system content 会 **TypeError**~~ ✅ 已在 commit 3fb8ae9 修复
- ~~`[conv]` `_filter_content_type` 仅浅拷贝，**污染调用方 messages**，多渠道 fallback 会传递被改过的请求~~ ✅ 修复于 2026-05-19
- ~~`[conv]` `to_chat.py` 与 `to_response.py` 的 **thinking budget→reasoning_effort 阈值不一致**~~ ✅ 修复于 2026-05-19（统一为 `converters/base.py:thinking_budget_to_effort`）
- ~~`[conv]` Anthropic→Response：tool_use + text 同一 message 时**输出顺序错乱**（function_call 跑到 text 前）~~ ✅ 修复于 2026-05-19
- ~~`[conv]` `total_tokens` 自己累加，**忽略 cached/reasoning tokens**，计费偏低~~ ✅ 修复于 2026-05-19 (commits d316eab..bc80014)


---

## 🟡 Minor（健壮性 / 可读性）

- ~~`[arch]` `state_store.get_response` 每次读都写盘更新 last_access_at（IO 放大）~~ ✅ 修复于 2026-05-19（改用 `os.utime` 仅更新 mtime，`evict_lru` 改读 mtime）
- ~~`[arch]` `_schedule_invalidate_model_channels_cache` fire-and-forget task **未保留强引用**（Python 3.11+ 警告）~~ ✅ 修复于 2026-05-19
- ~~`[arch]` `proxy_core.py:339` `chatcmpl-{model[:8]}` 兜底 ID **不唯一**~~ ✅ 修复于 2026-05-19（改用 `secrets.token_hex(12)`）
- ~~`[sec]` `serve_viewer.py` `Access-Control-Allow-Origin: *` 且无认证~~ ✅ 修复于 2026-05-19（移除 CORS 通配 + 绑定 127.0.0.1）
- ~~`[sec]` `stats.py:32-34` `datetime.utcnow()` 已弃用，落库 naive 时间无 tz~~ ✅ 修复于 2026-05-19
- ~~`[conv]` `to_anthropic.py:115-120` `_convert_content` 分支顺序错误，list 中纯字符串元素被静默丢弃~~ ✅ 修复于 2026-05-19
- ~~`[conv]` `to_response.py:794-795` `response.in_progress.response.model` 写死空字符串~~ ✅ 修复于 2026-05-19
- ~~`[conv]` `to_response.py:917` `function_call_arguments.done` 缺 `name` 字段~~ ✅ 修复于 2026-05-19
- ~~`[conv]` `_tool_result_to_chat_message` 把 image content 静默吞掉~~ ✅ 修复于 2026-05-19（转为 `[Image: ...]` 占位符）
