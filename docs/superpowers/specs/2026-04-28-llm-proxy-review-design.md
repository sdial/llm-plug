# LLM-API 转换器 Code Review 与兼容性改进计划

> **目标：** 验证 Claude Code (Anthropic) 和 OpenCode (OpenAI) 在现有架构下的兼容性，并进行代码质量改进

**架构概述：** 
- 入口：Claude Code 使用 Anthropic 格式，OpenCode 使用 OpenAI 兼容格式
- 出口：上游渠道混合（Anthropic/OpenAI），通过渠道配置标识
- 核心：根据入口格式和目标渠道类型自动选择转换器

**技术栈：** Python 3.11+, FastAPI, httpx, Pydantic

---

## 一、现状分析

### 1.1 转换矩阵（已实现）

| 入口格式 | Anthropic 渠道 | OpenAI 渠道 |
|---------|---------------|-------------|
| Anthropic | 直接透传 | Anthropic→OpenAI 转换 |
| OpenAI | OpenAI→Anthropic 转换 | 直接透传 |

### 1.2 已发现的问题

| 优先级 | 问题 | 位置 | 描述 |
|-------|------|------|------|
| 高 | 加权轮询算法错误 | `balancer/load_balancer.py` | 只递减选中channel的weight |
| 高 | 流式响应内存累积 | `proxy_core.py` | stream_chunks列表无限增长 |
| 中 | JSON解析无错误处理 | `routers/proxy_base.py` | request.json()缺少try-except |
| 中 | 硬编码超时时间 | `client.py` | 300秒超时不可配置 |
| 低 | 时间源不一致 | `storage.py` | monotonic与time混用 |
| 低 | 连接池无限制 | `client.py` | 无最大连接数控制 |

### 1.3 兼容性测试场景

```
场景 1: Claude Code → Anthropic渠道
  请求: POST /v1/messages (Anthropic格式)
  渠道: api_type=anthropic
  预期: 直接透传，response直接返回

场景 2: Claude Code → OpenAI渠道
  请求: POST /v1/messages (Anthropic格式)
  渠道: api_type=openai-chat-completions
  预期: Anthropic→OpenAI转换，response转换回Anthropic

场景 3: OpenCode → OpenAI渠道
  请求: POST /v1/chat/completions (OpenAI格式)
  渠道: api_type=openai-chat-completions
  预期: 直接透传

场景 4: OpenCode → Anthropic渠道
  请求: POST /v1/chat/completions (OpenAI格式)
  渠道: api_type=anthropic
  预期: OpenAI→Anthropic转换

场景 5: 流式响应 (所有场景)
  预期: 流式数据正确转换，SSE格式正确
```

---

## 二、测试环境设计

### 2.1 测试脚本结构

```
tests/
├── __init__.py
├── conftest.py                    # pytest配置和fixtures
├── fixtures/
│   ├── anthropic_request.json     # Claude Code请求样例
│   ├── openai_chat_request.json    # OpenCode请求样例
│   └── mock_channels.json         # 测试用渠道配置
├── test_anthropic_to_anthropic.py # 场景1测试
├── test_anthropic_to_openai.py    # 场景2测试
├── test_openai_to_openai.py       # 场景3测试
├── test_openai_to_anthropic.py    # 场景4测试
├── test_streaming/
│   ├── test_anthropic_stream.py
│   ├── test_openai_stream.py
│   └── test_stream_conversion.py
└── test_load_balancer.py          # 负载均衡测试
```

### 2.2 测试数据

**Anthropic请求样例 (Claude Code风格):**
```json
{
  "model": "claude-sonnet-4-20250514",
  "messages": [
    {"role": "user", "content": "Hello, explain quantum computing"}
  ],
  "max_tokens": 1024,
  "thinking": {
    "type": "enabled",
    "budget_tokens": 1000
  }
}
```

**OpenAI Chat请求样例 (OpenCode风格):**
```json
{
  "model": "gpt-4o",
  "messages": [
    {"role": "user", "content": "Hello, explain quantum computing"}
  ],
  "max_tokens": 1024
}
```

---

## 三、修复计划

### 3.1 高优先级修复

1. **修复加权轮询算法**
   - 问题：只递减选中channel的weight
   - 修复：递减所有channels的weight

2. **添加流式响应限制**
   - 问题：stream_chunks无限增长
   - 修复：添加最大chunk数量限制或使用生成器

3. **添加请求体验证**
   - 问题：JSON解析无错误处理
   - 修复：添加try-except，返回400错误

### 3.2 中优先级修复

4. **超时时间可配置化**
   - 添加环境变量 `REQUEST_TIMEOUT`
   - 支持不同渠道的自定义超时

5. **修复时间源一致性**
   - 统一使用 `time.time()`

### 3.3 代码质量改进

6. **添加类型注解**
   - 补充缺失的类型注解

7. **添加更多边界处理**
   - 空消息列表
   - 缺失字段的默认值

8. **改进错误信息**
   - 区分不同类型的转换错误

---

## 四、兼容性增强

### 4.1 Anthropic 特有字段支持

| 字段 | Claude Code | 当前支持 | 备注 |
|-----|------------|---------|------|
| `thinking` | ✅ | ✅ | thinking块转换 |
| `budget_tokens` | ✅ | ✅ | 预算token设置 |
| `system` | ✅ | ⚠️ | 需要转换为messages |
| `tools` | ✅ | ✅ | 工具调用转换 |
| `tool_choice` | ✅ | ✅ | 工具选择转换 |
| `stream` | ✅ | ✅ | 流式响应 |

### 4.2 OpenAI 特有字段支持

| 字段 | OpenCode | 当前支持 | 备注 |
|-----|---------|---------|------|
| `tools` | ✅ | ✅ | 工具调用转换 |
| `tool_choice` | ✅ | ✅ | 工具选择转换 |
| `functions` | ⚠️ | ✅ | 旧版function calling |
| `response_format` | ✅ | ⚠️ | json_schema支持 |
| `stream` | ✅ | ✅ | 流式响应 |

---

## 五、实施步骤

### 阶段 1: 测试环境搭建
1. 创建测试目录结构
2. 编写测试fixtures
3. 创建mock服务器
4. 编写基础测试用例

### 阶段 2: 核心问题修复
1. 修复加权轮询算法
2. 添加流式响应限制
3. 添加请求体验证

### 阶段 3: 兼容性验证
1. 运行场景1-4测试
2. 运行流式响应测试
3. 修复发现的问题

### 阶段 4: 代码质量提升
1. 超时时间可配置化
2. 时间源一致性修复
3. 类型注解补充

---

## 六、验收标准

- [ ] 所有测试场景通过
- [ ] Claude Code 可正常连接并使用
- [ ] OpenCode 可正常连接并使用
- [ ] 流式响应正常工作
- [ ] 负载均衡算法正确
- [ ] 代码无明显问题
