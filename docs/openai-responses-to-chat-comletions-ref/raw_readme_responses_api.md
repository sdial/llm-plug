# Responses API 摘要

> 来源：https://github.com/openai/openai-python/blob/master/README.md

## 基本格式

Responses API 使用 `client.responses.create()` 方法，主要参数包括：

- **model**: 模型名称（如 "gpt-5.2"）
- **instructions**: 系统级指令，设置助手角色
- **input**: 输入内容，支持多种格式

---

## Input 字段结构

input 参数支持数组格式，每个元素包含：

- **role**: 角色（如 "user"）
- **content**: 内容数组，支持多种类型：
  - `{"type": "input_text", "text": "..."}` — 文本输入
  - `{"type": "input_image", "image_url": "..."}` — 图像输入（URL或base64）

---

## 输出字段

响应对象提供 `output_text` 属性获取生成的文本。

---

## 与 Chat Completions 的主要差异

| 特性 | Responses API | Chat Completions API |
|------|--------------|---------------------|
| 调用方式 | `client.responses.create()` | `client.chat.completions.create()` |
| 系统指令 | `instructions` 参数 | messages 中使用 `{"role": "developer"}` |
| 输入格式 | `input` 参数 | `messages` 参数 |
| 文本类型 | `input_text` | 纯文本字符串 |
| 输出访问 | `response.output_text` | `completion.choices[0].message.content` |
| 状态说明 | "The primary API for interacting with OpenAI models" | "The previous standard (supported indefinitely)" |

---

## 流式响应

两者都支持 `stream=True` 参数，接口一致：

```python
stream = client.responses.create(model="gpt-5.2", input="...", stream=True)
for event in stream:
    print(event)
```

---

## 注意事项

README 中明确指出 Chat Completions 是"the previous standard (supported indefinitely)"，而 Responses API 被定位为主要交互方式。但文档未提供 Responses API 完整字段定义，完整 API 需参考 `api.md` 文件。
