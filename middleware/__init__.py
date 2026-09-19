"""middleware 包 — 5 个纯 ASGI 中间件（文件名即职责）。

- whitelist_middleware:  IP 白名单（403）
- admin_auth_middleware: admin 会话 + 重定向（401/302）
- proxy_auth_middleware: 代理鉴权（401/403 + allowed_models）
- body_buffer_middleware: body 解析 + 413 体积上限 + 非代理路径直通
- request_log_middleware: 请求/响应日志
- common: 共享 helper

链序（执行顺序）：Whitelist → AdminAuth → ProxyAuth → BodyBuffer → RequestLog → app。
共享状态经 scope["state"] 传递（client_ip / original_path / body_bytes / model /
stream / auth_result / proxy_auth_checked / api_key_id）。
"""
