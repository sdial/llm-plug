<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-24 | Updated: 2026-04-24 -->

# converters

## Purpose
API 格式转换器模块，实现 OpenAI Chat Completions、OpenAI Response、Anthropic 三种格式之间的相互转换。

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | 模块初始化 (空文件) |
| `base.py` | BaseConverter 抽象基类，定义转换接口 |
| `to_chat.py` | ToChatCompletionsConverter：任意格式 → OpenAI Chat Completions |
| `to_response.py` | ToResponseConverter：任意格式 → OpenAI Response |
| `to_anthropic.py` | ToAnthropicConverter：任意格式 → Anthropic Messages |

## Subdirectories
无

## For AI Agents

### Working In This Directory
- 新增转换器需继承 BaseConverter 并实现三个抽象方法
- 转换器需处理请求体、响应体、流式响应块三种数据
- 注意保持 token 使用量统计的准确性

### Testing Requirements
- 测试每种格式的请求和响应转换
- 验证流式响应块的转换正确性
- 测试多模态内容 (图片) 的转换
- 测试工具调用 (tool_calls) 的转换

### Common Patterns
- 策略模式：根据 source_type 选择具体转换方法
- 模板方法：基类定义接口，子类实现具体转换
- 私有方法命名：`_{source}_to_{target}` 格式

## Dependencies

### Internal
- `converters/base.py` - BaseConverter 基类

### External
无 (仅使用标准库)

<!-- MANUAL: -->
