# RESPONSES -> CHAT COMPLETIONS

太棒了！我找到了**两个非常符合你需求的项目**，它们都支持 **Responses API → Chat Completions** 的转换：

---

## 1. SSebo/codex-responses-adapter ⭐ 强烈推荐

- **语言**: Rust
- **项目地址**: https://github.com/SSebo/codex-responses-adapter
- **核心功能**:
  - ✅ **Responses API → Chat Completions** 转换
  - ✅ 支持流式和非流式响应
  - ✅ 支持 Function tool 调用
  - ✅ 多提供商路由与自动降级
  - ✅ 内置 Web Search 工具 (Tavily, Brave, 自定义后端)
  - ✅ 模型名称映射
  - ✅ 37 个单元测试，维护活跃 (2026年3月更新)

**简介**: 专为 Codex CLI 用户设计，完美适配你的场景 - Codex 发送 Responses API 请求，适配器转换为 Chat Completions 格式与上游通信。

---

## 2. Kurok1/openai-responses-adapter

- **语言**: Go
- **项目地址**: https://github.com/Kurok1/openai-responses-adapter
- **核心功能**:
  - ✅ **Responses API → Chat Completions** 转换
  - ✅ `previous_response_id` 多轮对话上下文链
  - ✅ MCP (Model Context Protocol) 工具桥接
  - ✅ 流式 SSE 响应
  - ✅ 内存响应存储 (TTL + LRU)
  - ✅ `developer` 角色自动降级为 `user`

**简介**: 轻量级 Go 代理，适合通用场景，支持 Docker 快速部署。

---

## 对比总结

| 特性 | SSebo/codex-responses-adapter | Kurok1/openai-responses-adapter |
|------|------------------------------|----------------------------------|
| 语言 | Rust | Go |
| 专注场景 | Codex CLI 用户 | 通用代理 |
| 多提供商支持 | ✅ 完整 | 基础 |
| MCP 工具 | ❌ | ✅ |
| Web Search | ✅ 多种后端 | ❌ |
| 单元测试 | 37个 | 较少 |
| 活跃度 | 高 (3月更新) | 中 (2月更新) |

---

## 建议

如果你的主要场景是**使用 Codex CLI**，我强烈推荐 **SSebo/codex-responses-adapter**，因为它：
1. 正是为 Codex + Chat Completions 上游场景设计的
2. 功能更完整，文档详尽
3. 测试覆盖率高，维护活跃

如果你需要**MCP 工具支持**或更**轻量级**的部署，可以选择 **Kurok1/openai-responses-adapter**。

两个项目都可以直接使用 Docker 部署，配置简单！
