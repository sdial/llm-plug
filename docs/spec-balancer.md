# spec-balancer — 负载均衡器

> 对应文件：`balancer/load_balancer.py`（约 119 行）

## 模块定位

负载均衡器负责从多个可用渠道中**选择一个**来处理请求。它实现了两层策略：
1. **优先级分组**：高优先级渠道优先使用
2. **加权轮询**：同优先级组内按权重分配流量

同时通过 `ChannelHealth` 实现渠道健康检查，自动剔除不健康渠道。

## 导入

```python
import storage  # 用于读取 lb_config（max_fail_count、cooldown_seconds）
```

## 全局单例

```python
load_balancer = LoadBalancer()  # 模块级单例
```

整个应用共享同一个 `LoadBalancer` 实例。

## ChannelHealth 类

跟踪单个渠道的健康状态。

```python
class ChannelHealth:
    fail_count: int = 0        # 连续失败次数
    last_fail_time: float = 0  # 上次失败时间戳
    current_weight: int = 0    # 当前轮询权重（SWRR 算法内部状态）
```

### 方法

| 方法 | 说明 |
|------|------|
| `record_success()` | 重置 `fail_count = 0` |
| `record_failure()` | `fail_count += 1`，记录 `last_fail_time` |
| `is_healthy(max_fail_count, cooldown_seconds)` | `fail_count < max_fail_count` 或冷却期已过 |

**健康判断逻辑**：

```
if fail_count < max_fail_count:   # 由 lb_config 动态提供，默认 5
    return True  # 还没连续失败够次数 → 健康
if (now - last_fail_time) > cooldown_seconds:  # 由 lb_config 动态提供，默认 60s
    return True  # 冷却期已过 → 恢复探测（健康）
return False  # 正在冷却中 → 不健康
```

> 注意：冷却期过后，`is_healthy` 返回 True，但 `fail_count` 并未重置。如果下一次请求又失败，`fail_count` 继续累加。只有成功一次后 `fail_count` 才归零。

## LoadBalancer 类

### 核心方法

#### `select_channel(channels, exclude_ids)`

```python
async def select_channel(
    channels: list[Channel],
    exclude_ids: set[str] | None = None,
) -> Optional[Channel]
```

**选择流程**：

1. **读取配置**：`config = await storage.get_lb_config()`，获取当前 `max_fail_count` 和 `cooldown_seconds`
2. **过滤**（在 `asyncio.Lock` 内原子执行）：排除 `enabled=False`、`id in exclude_ids`、`is_healthy(config.max_fail_count, config.cooldown_seconds)=False` 的渠道
3. **分组**：按 `priority` 升序排序（数字越小优先级越高），取最小 priority 的组
4. **选择**：
   - 组内只有 1 个 → 直接返回
   - 组内多个 → 调用 `_weighted_round_robin()`

#### `_weighted_round_robin(channels)`

**平滑加权轮询算法**（Smooth Weighted Round-Robin，类似 Nginx SWRR）。

```
算法步骤：
1. 每个 channel 的 current_weight += weight
2. 选择 current_weight 最大的 channel
3. 被选中 channel 的 current_weight -= total_weight
```

**示例**：假设三个渠道 A(weight=5)、B(weight=1)、C(weight=1)

| 轮次 | A(cw) | B(cw) | C(cw) | 选中 | A(cw') | B(cw') | C(cw') |
|------|-------|-------|-------|------|--------|--------|--------|
| 1 | 5 | 1 | 1 | A | -2 | 1 | 1 |
| 2 | 3 | 2 | 2 | A | -4 | 2 | 2 |
| 3 | 1 | 3 | 3 | B | 1 | -4 | 3 |
| 4 | 6 | -3 | 4 | A | -1 | -3 | 4 |
| 5 | 4 | -2 | 5 | C | 4 | -2 | -2 |
| 6 | 9 | -1 | -1 | A | 2 | -1 | -1 |
| 7 | 7 | 0 | 0 | A | 0 | 0 | 0 |

7 轮中 A 被选 5 次，B 被选 1 次，C 被选 1 次，比例精确为 5:1:1。

### 健康记录方法

| 方法 | 说明 |
|------|------|
| `record_success(channel_id)` | 标记渠道成功，重置 fail_count |
| `record_failure(channel_id)` | 标记渠道失败，累加 fail_count |
| `get_health(channel_id)` | 获取渠道的 ChannelHealth 实例 |
| `cleanup_removed_channels(active_channel_ids)` | 清理已删除渠道的健康状态记录。在 `_invalidate_model_channels_cache()` 中被调用 |

## 配置项

| 配置字段 | 默认值 | 说明 |
|----------|--------|------|
| `lb_config.max_fail_count` | 5 | 连续失败 N 次后标记渠道不健康 |
| `lb_config.cooldown_seconds` | 60 | 不健康渠道冷却恢复时间（秒） |

以上配置存储在 `channels.json` 的 `lb_config` 字段中，通过 `storage.get_lb_config()` 读取（带 5 秒 TTL 缓存）。可在运行时通过管理 API 修改，无需重启服务。

## 线程安全

- 使用 `asyncio.Lock`（不是 `threading.Lock`），因为 `select_channel` 是 `async` 方法
- 整个选择过程在锁内完成，确保健康检查与轮询的原子性
- `record_success` / `record_failure` 不加锁（`defaultdict` 的操作是原子的，且精度要求不高）

## 在 proxy_core 中的使用

```python
# 选择渠道
selected = await load_balancer.select_channel(channels, exclude_ids=all_tried)

# 成功
load_balancer.record_success(channel.id)

# 失败
load_balancer.record_failure(channel.id)
```

## 注意事项

1. **priority 数字越小优先级越高**：1 比 2 优先。默认值为 1。
2. **冷却期只是恢复探测**：冷却过后 `is_healthy` 返回 True，允许再试一次。如果继续失败，fail_count 会继续增长。
3. **current_weight 不重置**：SWRR 的 current_weight 是持续累积的，这保证了长时间运行的流量分配比例精确。
4. **单例共享**：所有请求共用同一个 `load_balancer`，`_health` 字典会随运行时间增长（但通常渠道数量有限，不构成问题）。
