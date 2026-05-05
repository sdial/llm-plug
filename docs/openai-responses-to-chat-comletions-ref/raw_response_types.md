# Response 类型定义

> 来源：https://raw.githubusercontent.com/openai/openai-python/master/src/openai/types/responses/response.py

## Response 类字段定义

### 必填字段

| 字段 | 类型 | 说明 |
|------|------|------|
| **id** | str | 响应的唯一标识符 |
| **created_at** | float | 创建时的Unix时间戳 |
| **model** | ResponsesModel | 模型ID，如 `gpt-4o` 或 `o3` |
| **object** | Literal["response"] | 对象类型，始终为 `response` |
| **output** | List[ResponseOutputItem] | 模型生成的内容项数组 |
| **parallel_tool_calls** | bool | 是否允许并行工具调用 |
| **tool_choice** | ToolChoice | 工具选择方式 |
| **tools** | List[Tool] | 模型可调用的工具数组 |

### 可选字段

| 字段 | 类型 | 说明 |
|------|------|------|
| **error** | Optional[ResponseError] | 错误对象 |
| **incomplete_details** | Optional[IncompleteDetails] | 响应不完整的原因 |
| **instructions** | Union[str, List[ResponseInputItem], None] | 系统消息 |
| **metadata** | Optional[Metadata] | 16个键值对的元数据 |
| **temperature** | Optional[float] | 采样温度 (0-2) |
| **top_p** | Optional[float] | 核采样参数 |
| **background** | Optional[bool] | 是否后台运行 |
| **completed_at** | Optional[float] | 完成时间戳 |
| **conversation** | Optional[Conversation] | 关联的会话 |
| **max_output_tokens** | Optional[int] | 最大输出token数 |
| **max_tool_calls** | Optional[int] | 最大内置工具调用次数 |
| **previous_response_id** | Optional[str] | 前一个响应ID |
| **prompt** | Optional[ResponsePrompt] | 提示模板引用 |
| **prompt_cache_key** | Optional[str] | 提示缓存键 |
| **prompt_cache_retention** | Optional[Literal["in_memory", "24h"]] | 缓存保留策略 |
| **reasoning** | Optional[Reasoning] | 推理配置 (gpt-5/o系列) |
| **safety_identifier** | Optional[str] | 安全标识符 |
| **service_tier** | Optional[Literal["auto", "default", "flex", "scale", "priority"]] | 服务层级 |
| **status** | Optional[ResponseStatus] | 状态 (completed/failed/in_progress/cancelled/queued/incomplete) |
| **text** | Optional[ResponseTextConfig] | 文本响应配置 |
| **top_logprobs** | Optional[int] | 返回最可能token数 (0-20) |
| **truncation** | Optional[Literal["auto", "disabled"]] | 截断策略 |
| **usage** | Optional[ResponseUsage] | token使用详情 |
| **user** | Optional[str] | 正被 `safety_identifier` 和 `prompt_cache_key` 替代 |

### 属性方法

- **output_text** (property): 聚合所有 `output_text` 项的便捷属性，返回字符串

---

## 完整类型列表（按功能分类）

### 核心响应类型
`Response`, `ResponseInput`, `ResponseOutputItem`, `ResponseItem`, `ResponseContent`, `ResponsePrompt`, `ResponseInputItem`, `ResponseStatus`, `ResponseError`, `ResponseUsage`

### 流式事件类型
`ResponseStreamEvent`, `ResponsesServerEvent`, `ResponsesClientEvent`

- 创建/状态事件：`ResponseCreatedEvent`, `ResponseInProgressEvent`, `ResponseCompletedEvent`, `ResponseFailedEvent`, `ResponseIncompleteEvent`, `ResponseQueuedEvent`, `ResponseErrorEvent`
- 输出项事件：`ResponseOutputItemAddedEvent`, `ResponseOutputItemDoneEvent`
- 内容部分事件：`ResponseContentPartAddedEvent`, `ResponseContentPartDoneEvent`
- 文本事件：`ResponseTextDeltaEvent`, `ResponseTextDoneEvent`, `ResponseRefusalDeltaEvent`, `ResponseRefusalDoneEvent`
- 音频事件：`ResponseAudioDeltaEvent`, `ResponseAudioDoneEvent`, `ResponseAudioTranscriptDeltaEvent`, `ResponseAudioTranscriptDoneEvent`
- 推理事件：`ResponseReasoningItem`, `ResponseReasoningTextDeltaEvent`, `ResponseReasoningTextDoneEvent`, `ResponseReasoningSummaryPartAddedEvent`, `ResponseReasoningSummaryPartDoneEvent`, `ResponseReasoningSummaryTextDeltaEvent`, `ResponseReasoningSummaryTextDoneEvent`
- 函数调用事件：`ResponseFunctionCallArgumentsDeltaEvent`, `ResponseFunctionCallArgumentsDoneEvent`

### 工具定义类型
`Tool`, `FunctionTool`, `FileSearchTool`, `WebSearchTool`, `WebSearchPreviewTool`, `ComputerTool`, `ComputerUsePreviewTool`, `CustomTool`, `ApplyPatchTool`, `FunctionShellTool`, `ToolSearchTool`, `NamespaceTool`, `InlineSkill`

### 工具调用结果类型
- 函数：`ResponseFunctionToolCall`, `ResponseFunctionToolCallItem`, `ResponseFunctionToolCallOutputItem`, `ResponseFunctionCallOutputItem`, `ResponseFunctionCallOutputItemList`
- 文件搜索：`ResponseFileSearchToolCall`, `ResponseFileSearchCallInProgressEvent`, `ResponseFileSearchCallSearchingEvent`, `ResponseFileSearchCallCompletedEvent`
- Web 搜索：`ResponseFunctionWebSearch`, `ResponseWebSearchCallInProgressEvent`, `ResponseWebSearchCallSearchingEvent`, `ResponseWebSearchCallCompletedEvent`
- 计算机：`ResponseComputerToolCall`, `ComputerAction`, `ComputerActionList`, `ResponseComputerToolCallOutputItem`, `ResponseComputerToolCallOutputScreenshot`
- 代码解释器：`ResponseCodeInterpreterToolCall`, `ResponseCodeInterpreterCallInProgressEvent`, `ResponseCodeInterpreterCallInterpretingEvent`, `ResponseCodeInterpreterCallCodeDeltaEvent`, `ResponseCodeInterpreterCallCodeDoneEvent`, `ResponseCodeInterpreterCallCompletedEvent`
- 图片生成：`ResponseImageGenCallInProgressEvent`, `ResponseImageGenCallGeneratingEvent`, `ResponseImageGenCallPartialImageEvent`, `ResponseImageGenCallCompletedEvent`
- ApplyPatch：`ResponseApplyPatchToolCall`, `ResponseApplyPatchToolCallOutput`
- Shell：`ResponseFunctionShellToolCall`, `ResponseFunctionShellToolCallOutput`, `ResponseFunctionShellCallOutputContent`
- 自定义工具：`ResponseCustomToolCall`, `ResponseCustomToolCallInputDeltaEvent`, `ResponseCustomToolCallInputDoneEvent`, `ResponseCustomToolCallItem`, `ResponseCustomToolCallOutput`, `ResponseCustomToolCallOutputItem`
- MCP：`ResponseMcpCallInProgressEvent`, `ResponseMcpCallArgumentsDeltaEvent`, `ResponseMcpCallArgumentsDoneEvent`, `ResponseMcpCallCompletedEvent`, `ResponseMcpCallFailedEvent`, `ResponseMcpListToolsInProgressEvent`, `ResponseMcpListToolsCompletedEvent`, `ResponseMcpListToolsFailedEvent`
- 工具搜索：`ResponseToolSearchCall`, `ResponseToolSearchOutputItem`, `ResponseToolSearchOutputItemParam`

### ToolChoice 类型
`ToolChoiceOptions`, `ToolChoiceTypes`, `ToolChoiceAllowed`, `ToolChoiceFunction`, `ToolChoiceCustom`, `ToolChoiceMcp`, `ToolChoiceApplyPatch`, `ToolChoiceShell`

### 输入类型
`ResponseInputText`, `ResponseInputTextContent`, `ResponseInputImage`, `ResponseInputImageContent`, `ResponseInputFile`, `ResponseInputFileContent`, `ResponseInputAudio`, `ResponseInputContent`, `ResponseInputMessageItem`, `ResponseInputMessageContentList`, `EasyInputMessage`, `ResponseConversationParam`

### 输出类型
`ResponseOutputMessage`, `ResponseOutputText`, `ResponseOutputAudio`, `ResponseOutputRefusal`, `ResponseOutputTextAnnotationAddedEvent`

### 格式配置
`ResponseTextConfig`, `ResponseFormatTextConfig`, `ResponseFormatTextJSONSchemaConfig`

### 容器/环境类型
`ContainerReference`, `ResponseContainerReference`, `ContainerAuto`, `LocalEnvironment`, `ResponseLocalEnvironment`, `ContainerNetworkPolicyAllowlist`, `ContainerNetworkPolicyDisabled`, `ContainerNetworkPolicyDomainSecret`

### 技能类型
`InlineSkillSource`, `LocalSkill`, `SkillReference`

### 压缩相关
`CompactedResponse`, `ResponseCompactionItem`, `ResponseCompactionItemParam`

### 其他
`ResponseIncludable`
