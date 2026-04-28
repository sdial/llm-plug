# spec-models — 数据模型

> 对应目录：`models/`（2 个文件）

## 模块定位

`models/` 定义了项目中的核心数据结构：API 格式枚举和渠道数据模型。这些模型贯穿整个项目，是所有模块之间传递数据的基础。

## api_types.py — API 格式枚举

### `APIType`

```python
class APIType(str, Enum):
    OPENAI_CHAT = "openai-chat-completions"
    OPENAI_RESPONSE = "openai-response"
    ANTHROPIC = "anthropic"
```

**继承 `str`**：这意味着 `APIType` 的值可以直接当字符串使用，例如：

```python
api_type = APIType.ANTHROPIC
print(api_type == "anthropic")  # True
print(api_type.value)           # "anthropic"
```

**使用场景**：

| 场景 | 用法 |
|------|------|
| 渠道的 `api_type` 字段 | `Channel.api_type: APIType` |
| 路由工厂的参数 | `make_proxy_router("/v1/messages", APIType.ANTHROPIC)` |
| 转换器的 `source_type` 参数 | 字符串值 `"anthropic"` / `"openai-chat-completions"` / `"openai-response"` |
| 上游 URL 拼接 | `if api_type == "openai-chat-completions": ...` |
| 上游认证头构建 | `if channel.api_type == APIType.ANTHROPIC: ...` |

**注意**：`proxy_core.py` 和转换器内部使用字符串值（`source_type`）而非枚举，这是因为转换器需要按字符串分支匹配。

## channel.py — 渠道数据模型

### `Channel` — 渠道完整模型

```python
class Channel(BaseModel):
    id: str = Field(default_factory=lambda: f"ch_{uuid.uuid4().hex[:8]}")
    name: str
    api_type: APIType
    base_url: str
    api_key: str
    models: list[str] = Field(default_factory=list)
    enabled: bool = True
    weight: int = Field(default=1, ge=1)
    priority: int = Field(default=1, ge=1)
    socks5_proxy: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
```

**字段说明**：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `id` | str | `ch_{uuid4_hex[:8]}` | 渠道唯一标识，自动生成 |
| `name` | str | 必填 | 渠道名称（如"OpenAI 官方"） |
| `api_type` | APIType | 必填 | 上游 API 格式类型 |
| `base_url` | str | 必填 | 上游 API 基础 URL（如 `https://api.openai.com`） |
| `api_key` | str | 必填 | 上游 API 密钥 |
| `models` | list[str] | `[]` | 此渠道支持的模型列表 |
| `enabled` | bool | `True` | 是否启用 |
| `weight` | int | `1` (≥1) | 负载均衡权重（越大分配越多流量） |
| `priority` | int | `1` (≥1) | 优先级（数字越小越优先） |
| `socks5_proxy` | str \| None | `None` | SOCKS5 代理地址 |
| `created_at` | str | 当前 UTC 时间 | 创建时间 |

### `ChannelCreate` — 创建渠道模型

```python
class ChannelCreate(BaseModel):
    name: str
    api_type: APIType
    base_url: str
    api_key: str
    models: list[str] = Field(default_factory=list)
    enabled: bool = True
    weight: int = Field(default=1, ge=1)
    priority: int = Field(default=1, ge=1)
    socks5_proxy: Optional[str] = None
```

与 `Channel` 的区别：
- 没有 `id`（自动生成）
- 没有 `created_at`（自动生成）

### `ChannelUpdate` — 更新渠道模型

```python
class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    api_type: Optional[APIType] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    models: Optional[list[str]] = None
    enabled: Optional[bool] = None
    weight: Optional[int] = None
    priority: Optional[int] = None
    socks5_proxy: Optional[str] = None
```

所有字段都是 `Optional`，仅更新传入的字段（`exclude_unset=True`）。

## 数据流转

```
客户端 POST /admin/channels (ChannelCreate)
  → Channel(**body.model_dump())     # 创建时生成 id 和 created_at
  → save_data({"channels": [ch.model_dump() for ch in channels]})

客户端 PUT /admin/channels/{id} (ChannelUpdate)
  → body.model_dump(exclude_unset=True)  # 只取传入的字段
  → ch.model_copy(update=update_data)    # 合并更新
  → save_data(...)

代理请求时:
  → load_data() → [Channel(**ch) for ch in data["channels"]]
  → 按 model 筛选 → 负载均衡 → 选择渠道
```

## 模型在存储中的序列化

通过 `channel.model_dump()` 序列化为 dict，再由 `json.dump()` 写入 `channels.json`。

```python
# 序列化
channel = Channel(name="test", api_type=APIType.OPENAI_CHAT, base_url="...", api_key="...")
data = channel.model_dump()
# {"id": "ch_abc12345", "name": "test", "api_type": "openai-chat-completions", ...}

# 反序列化
channel = Channel(**data)
```

**注意**：`APIType` 枚举序列化为其字符串值（如 `"openai-chat-completions"`），反序列化时自动还原。

## Pydantic V2 特性

- `model_dump()` 替代 V1 的 `dict()`
- `model_dump(exclude_unset=True)` 只包含显式设置的字段
- `model_copy(update={...})` 创建副本并更新指定字段
- `Field(default_factory=...)` 用于可变默认值（如 list）

## 注意事项

1. **id 格式**：`ch_` + 8 位十六进制，如 `ch_a1b2c3d4`。8 位 hex 有 4 billion 种组合，对于手动管理的渠道数量足够。
2. **weight 和 priority 的区别**：
   - `weight`：同一优先级组内的流量分配比例
   - `priority`：决定使用哪个优先级组（数字越小越优先）
   - 例：priority=1 的渠道总会被优先选择，只有它们全部不可用时才会选 priority=2 的
3. **models 列表为空**：`models=[]` 的渠道不会被任何代理请求选中（因为按 model 匹配），但可以通过连通性测试接口测试。
4. **socks5_proxy 格式**：标准代理 URL，如 `socks5://user:pass@host:port`。传入 None 或空表示不使用代理。
