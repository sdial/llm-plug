# 模型组 Fallback 功能设计

## 背景

当前系统支持渠道级别的负载均衡和故障转移，但缺少模型级别的互通能力。用户希望：
1. 让能力相近的模型可以互通（如 glm5.1 和 glm5.0）
2. 模型之间按优先级 Fallback，而非同时负载均衡
3. 通过后台界面配置，无需修改代码

## 设计目标

- 新增"模型组"概念，组内模型按顺序 Fallback
- 保持现有渠道级负载均衡逻辑不变
- 后台可配置全局参数（失败次数阈值、冷却时间）

## 核心逻辑

### 两级 Fallback 架构

```
请求 "glm5-group"
    │
    ├─ 模型层 Fallback：glm5.1 → glm5.0（glm5.1 全失败才切 glm5.0）
    │     │
    │     └─ 渠道层负载均衡：支持 glm5.1 的渠道 A、B、C 之间按 priority/weight 分配
    │
    └─ 全部失败 → 返回错误
```

### 层级职责

| 层级 | 行为 | 原因 |
|------|------|------|
| 模型层 | Fallback（按配置顺序） | 不同模型能力不同，优先用强的 |
| 渠道层 | 负载均衡（现有逻辑） | 同模型的渠道是等价的，分担负载 |

## 数据结构

### channels.json 新增字段

```json
{
  "channels": [...],
  "model_groups": [
    {
      "id": "grp_xxx",
      "name": "glm5-group",
      "models": ["glm5.1", "glm5.0"],
      "enabled": true,
      "created_at": "2026-05-02T..."
    }
  ],
  "lb_config": {
    "max_fail_count": 5,
    "cooldown_seconds": 60
  }
}
```

### 字段说明

**model_groups**：
- `id`：组唯一标识，格式 `grp_{uuid4_hex[:8]}`
- `name`：组名，客户端请求时使用此名称
- `models`：模型列表，顺序即为 Fallback 顺序
- `enabled`：是否启用
- `created_at`：创建时间

**lb_config**：
- `max_fail_count`：连续失败 N 次后标记渠道不健康（默认 5）
- `cooldown_seconds`：不健康渠道冷却恢复时间（默认 60 秒）

## 请求处理流程

### proxy_core.py 改动

```
1. 接收请求，获取 model 参数
2. 查询 model 是否为模型组名
   - 是组名 → 调用 select_channel_for_group()
   - 不是组名 → 走现有 select_channel() 逻辑
3. 执行请求，返回响应
```

### LoadBalancer 新增方法

```python
async def select_channel_for_group(
    self,
    group_name: str,
    all_channels: list[Channel],
    exclude_model_channels: dict[str, set[str]],  # 已失败的渠道ID，按模型分组
) -> tuple[Channel, str] | None:
    """
    为模型组选择渠道
    返回
    """
    group = get_model_group(group_name)
    for model in group.models:
        channels = [ch for ch in all_channels if model in ch.models]
        tried = exclude_model_channels.get(model, set())
        selected = await self.select_channel(channels, exclude_ids=tried)
        if selected:
            return selected, model
    return None
```

### Fallback 调用流程

```python
async def proxy_request(...):
    group = get_model_group(model)
    if group:
        # 模型组请求
        all_tried: dict[str, set[str]] = {}  # model -> set[channel_id]
        for current_model in group.models:
            channels = get_channels_for_model(current_model)
            tried = all_tried.get(current_model, set())
            selected = await load_balancer.select_channel(channels, exclude_ids=tried)
            if not selected:
                continue  # 该模型无可用渠道，切换下一个模型
            try:
                return await do_request(selected, ...), selected
            except RetryableException:
                load_balancer.record_failure(selected.id)
                all_tried.setdefault(current_model, set()).add(selected.id)
                # 继续尝试同模型的其他渠道
                # 同模型渠道全失败后，for 循环会切换到下一个模型
        raise ValueError("模型组所有渠道均不可用")
    else:
        # 现有逻辑：单模型请求
        ...
```

## 后台 API

### 模型组 CRUD

| 接口 | 方法 | 说明 |
|------|------|------|
| `/admin/model-groups` | GET | 获取所有模型组 |
| `/admin/model-groups` | POST | 创建模型组 |
| `/admin/model-groups/{id}` | PUT | 更新模型组 |
| `/admin/model-groups/{id}` | DELETE | 删除模型组 |

### 全局参数配置

| 接口 | 方法 | 说明 |
|------|------|------|
| `/admin/lb-config` | GET | 获取负载均衡配置 |
| `/admin/lb-config` | PUT | 更新负载均衡配置 |

### 请求/响应示例

**创建模型组**：
```json
// POST /admin/model-groups
{
  "name": "glm5-group",
  "models": ["glm5.1", "glm5.0"],
  "enabled": true
}

// Response
{
  "id": "grp_a1b2c3d4",
  "name": "glm5-group",
  "models": ["glm5.1", "glm5.0"],
  "enabled": true,
  "created_at": "2026-05-02T..."
}
```

**更新全局参数**：
```json
// PUT /admin/lb-config
{
  "max_fail_count": 3,
  "cooldown_seconds": 30
}
```

## 前端页面

### 新增"负载均衡"标签页

**布局**：

```
┌─────────────────────────────────────────────────────────┐
│ 全局参数                                                 │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ 失败次数阈值: [5]  冷却时间(秒): [60]  [保存]       │ │
│ └─────────────────────────────────────────────────────┘ │
│                                                         │
│ 模型组                                     [+ 添加模型组] │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ 名称          │ 模型列表           │ 状态 │ 操作    │ │
│ │ glm5-group    │ glm5.1 → glm5.0    │ 启用 │ 编辑 删除│ │
│ │ ds-group      │ ds-v4 → ds-v3      │ 启用 │ 编辑 删除│ │
│ └─────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

**添加/编辑模型组弹窗**：
- 组名（必填）
- 模型选择（支持添加多个，可拖拽排序）
- 启用状态

## 文件改动清单

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `models/model_group.py` | 新增 | ModelGroup 数据模型 |
| `models/channel.py` | 修改 | 添加 ModelGroup 引用（可选） |
| `storage.py` | 修改 | 支持 model_groups 和 lb_config 读写 |
| `config.py` | 修改 | 删除环境变量，改为从 storage 读取 |
| `balancer/load_balancer.py` | 修改 | 新增 select_channel_for_group 方法 |
| `proxy_core.py` | 修改 | 支持模型组请求 |
| `routers/admin.py` | 修改 | 新增模型组和全局参数 API |
| `static/index.html` | 修改 | 新增负载均衡标签页 |

## 兼容性

- 现有单模型请求逻辑不变
- 未配置模型组时，行为与现在完全一致
- `lb_config` 不存在时使用默认值

## 测试要点

1. 模型组请求：组名正确匹配，Fallback 顺序正确
2. 渠道级 Fallback：同模型多渠道失败切换
3. 模型级 Fallback：所有渠道失败后切换模型
4. 全局参数：动态修改后立即生效
5. 边界情况：空组、单模型组、禁用组
