<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-24 | Updated: 2026-04-24 -->

# models

## Purpose
数据模型定义模块，包含渠道 (Channel) 和 API 类型 (APIType) 的 Pydantic 模型定义，用于请求验证和数据序列化。

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | 模块初始化 (空文件) |
| `api_types.py` | APIType 枚举定义：OPENAI_CHAT, OPENAI_RESPONSE, ANTHROPIC |
| `channel.py` | Channel, ChannelCreate, ChannelUpdate 模型定义 |

## Subdirectories
无

## For AI Agents

### Working In This Directory
- 新增 API 类型需修改 `api_types.py` 的枚举
- 修改渠道字段需同步更新 Channel, ChannelCreate, ChannelUpdate 三个模型
- 字段变更需考虑向后兼容性

### Testing Requirements
- 模型修改后验证 Pydantic 验证逻辑
- 检查 JSON 序列化/反序列化

### Common Patterns
- 使用 Pydantic BaseModel 进行数据验证
- Field() 用于默认值和约束
- Optional[] 用于可空字段

## Dependencies

### Internal
无

### External
- `pydantic` - 数据验证框架

<!-- MANUAL: -->
