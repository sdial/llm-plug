# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

LLM API 转换器 — 支持两种 LLM API 格式互转（OpenAI Chat Completions、Anthropic），带负载均衡与故障转移，Response格式仅透传。

## 原则
- 只有重复5次以上的代码才需要提取为独立函数，其他函数直接调用。
- 可读性和架构清晰性优先，避免过深的嵌套和复杂的逻辑。

## 常用命令

```bash
# 安装依赖
uv sync

# 启动服务（开发模式，热重载）
uv run uvicorn main:app --host 0.0.0.0 --port 55555 --reload

# 启动服务（Windows 推荐，避免端口释放问题）
uv run python main.py --no-reload

# 运行所有测试
uv run pytest

# 运行单个测试
uv run pytest tests/converters/test_converter_matrix.py -v

# 代码检查
uv run ruff check .
```

## 核心架构

### 请求处理流程

1. `main.py` → CombinedMiddleware（认证 + 日志）
2. `proxy_core.py:proxy_request()` → 兼容入口，实际进入 `proxy/core.py` 做模型路由 + 负载均衡选择渠道
3. `proxy/core.py:_do_request()` → 格式转换 → 上游请求 → 格式转换 → 返回响应

### 关键模块

| 模块 | 职责 |
|------|------|
| `proxy_core.py` | 兼容门面：导入时指向 `proxy.core`，保留旧 import / monkeypatch 路径 |
| `proxy/core.py` | 代理调度主流程：模型路由、负载均衡调用、请求执行、流式执行 |
| `proxy/channel_registry.py` | 模型到渠道的缓存、保存回调、删除渠道后的 LB 清理 |
| `proxy/conversion.py` | `CONVERTER_MAP`、转换器选择、跨格式渠道过滤、Responses 历史展开 |
| `proxy/stream_sse.py` | SSE 解析/格式化、Anthropic 事件合成、非 SSE JSON 转流式事件 |
| `proxy/stream_reconstruct.py` | 从流式 chunks 重建完整响应体，供请求日志保存 |
| `converters/` | 格式转换器（`to_chat.py`, `to_response.py`, `to_anthropic.py`） |
| `balancer/load_balancer.py` | 加权轮询 + 优先级分组 + 健康检查 |
| `client.py` | HTTP 客户端缓存池，支持 SOCKS5 代理 |
| `config.py` | 零配置默认值 + `data/settings.json` 配置管理 |
| `storage.py` | JSON 文件读写（`channels.json`, `api_keys.json`） |

### 格式转换路由

`proxy/conversion.py:CONVERTER_MAP` 定义 6 种转换方向，并通过 `proxy_core.CONVERTER_MAP` 兼容暴露：
- OpenAI Chat ↔ Anthropic
- OpenAI Response ↔ Anthropic
- OpenAI Chat ↔ OpenAI Response

同格式请求（如 OpenAI Chat → OpenAI Chat 渠道）直接透传，不做转换。

### 负载均衡策略

`LoadBalancer.select_channel()`：
1. 按优先级分组（priority 数字越小越优先）
2. 最高优先级组内加权轮询（weight）
3. 失败渠道冷却恢复（max_fail_count + cooldown_seconds）

## 测试结构

- `tests/converters/` — 格式转换器测试矩阵
- `tests/balancer/` — 负载均衡逻辑测试
- `tests/routers/` — 路由测试
- `tests/test_e2e.py` — 端到端测试（需 mock server）

## 数据模型

渠道配置存储于 `data/channels.json`，每个渠道包含：
- `api_type`: 渠道上游 API 格式
- `models`: 支持的模型列表
- `weight`/`priority`: 负载均衡参数
- `socks5_proxy`: 可选代理

模型组存储于 `data/model_groups.json`，支持 Fallback 顺序。

## 配置

项目不依赖 `.env`。服务固定监听 `0.0.0.0:55555`，Docker 对外端口由 ports 映射处理。业务配置通过前端设置页保存到 `data/settings.json`：
- 请求超时、请求体大小
- 负载均衡失败阈值和冷却时间
- 日志级别
- 请求记录数据库与 RAW 信息保存开关
