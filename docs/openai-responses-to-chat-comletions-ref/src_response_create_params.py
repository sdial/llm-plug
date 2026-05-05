# File generated from our OpenAPI spec by Stainless. See CONTRIBUTING.md for details.
# ResponseCreateParams - 请求参数定义
# 核心字段摘要:
# - model: 模型ID (必填)
# - input: 输入内容 (文本/图片/文件)
# - instructions: 系统消息
# - previous_response_id: 前一次响应ID (用于多轮对话)
# - conversation: 会话归属
# - tools: 工具数组
# - tool_choice: 工具选择方式
# - reasoning: 推理配置 (gpt-5/o系列)
# - stream: 是否流式
# 
# 完整类型定义请参考官方 SDK:
# https://github.com/openai/openai-python

from typing import List, Union, Iterable, Optional
from typing_extensions import Literal, Required, TypeAlias, TypedDict

# 关键类型: previous_response_id 和 conversation 用于会话管理
# 这两个字段是 Response API 有状态特性的核心
# Chat Completions API 没有对应字段，转换时会丢失会话状态

class ContextManagement(TypedDict, total=False):
    type: Required[str]
    """The context management entry type. Currently only 'compaction' is supported."""
    compact_threshold: Optional[int]
    """Token threshold at which compaction should be triggered."""


class StreamOptions(TypedDict, total=False):
    """Options for streaming responses."""
    include_obfuscation: bool
    """When true, stream obfuscation will be enabled."""


class ResponseCreateParamsBase(TypedDict, total=False):
    # === 核心字段 ===
    model: str  # ResponsesModel
    """Model ID used to generate the response, like `gpt-4o` or `o3`."""
    
    input: Union[str, list]
    """Text, image, or file inputs to the model."""
    
    instructions: Optional[str]
    """A system (or developer) message inserted into the model's context."""
    
    # === 会话管理字段 (Chat Completions 无对应) ===
    previous_response_id: Optional[str]
    """The unique ID of the previous response. Use this to create multi-turn conversations.
    CANNOT be used in conjunction with `conversation`."""
    
    conversation: Optional[Union[str, dict]]
    """The conversation that this response belongs to.
    Items from this conversation are prepended to input_items."""
    
    # === 工具配置 ===
    tools: Iterable[dict]
    """An array of tools the model may call.
    Supports: function, file_search, web_search, computer_use, code_interpreter, 
    image_gen, apply_patch, shell, mcp, custom"""
    
    tool_choice: Union[str, dict]
    """How the model should select which tool (or tools) to use."""
    
    parallel_tool_calls: Optional[bool]
    """Whether to allow the model to run tool calls in parallel."""
    
    # === 输出控制 ===
    max_output_tokens: Optional[int]
    """An upper bound for the number of tokens that can be generated."""
    
    max_tool_calls: Optional[int]
    """The maximum number of total calls to built-in tools."""
    
    temperature: Optional[float]
    """What sampling temperature to use, between 0 and 2."""
    
    top_p: Optional[float]
    """An alternative to sampling with temperature."""
    
    # === 推理配置 ===
    reasoning: Optional[dict]
    """**gpt-5 and o-series models only** Configuration options for reasoning models."""
    
    # === 流式 ===
    stream: Optional[bool]
    """If set to true, the model response data will be streamed."""
    
    stream_options: Optional[StreamOptions]
    """Options for streaming responses."""
    
    # === 其他 ===
    background: Optional[bool]
    """Whether to run the model response in the background."""
    
    context_management: Optional[Iterable[ContextManagement]]
    """Context management configuration for this request."""
    
    store: Optional[bool]
    """Whether to store the generated model response for later retrieval."""
    
    truncation: Optional[Literal["auto", "disabled"]]
    """The truncation strategy to use for the model response."""
    
    metadata: Optional[dict]
    """Set of 16 key-value pairs that can be attached to an object."""
    
    service_tier: Optional[Literal["auto", "default", "flex", "scale", "priority"]]
    """Specifies the processing type used for serving the request."""
    
    safety_identifier: Optional[str]
    """A stable identifier for detecting policy violations."""
    
    user: Optional[str]
    """This field is being replaced by `safety_identifier` and `prompt_cache_key`."""


class ResponseCreateParamsNonStreaming(ResponseCreateParamsBase, total=False):
    stream: Optional[Literal[False]]


class ResponseCreateParamsStreaming(ResponseCreateParamsBase):
    stream: Required[Literal[True]]


ResponseCreateParams = Union[ResponseCreateParamsNonStreaming, ResponseCreateParamsStreaming]
