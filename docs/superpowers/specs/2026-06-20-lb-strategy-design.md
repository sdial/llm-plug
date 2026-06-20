# 负载均衡策略扩展设计

## 概述

为负载均衡模块引入三种可选策略，替代当前硬编码的加权轮询算法。用户在设置页「负载均衡」TAB 通过下拉框选择全局策略。

## 策略定义

| 配置值 | 策略名 | 行为 |
|--------|--------|------|
| `round_robin` | 加权轮询 | **当前默认行为**。同优先级内按权重 SWRR 分发，低优先级为备份 |
| `backup` | 顺序备份 | 按优先级排序，始终选择最靠前的健康渠道；失败才切下一个 |
| `sticky` | 会话粘性 | 按复合会话标识一致性哈希到固定渠道，同一会话始终打到同一上游 |

## 配置

### settings.json

```json
{
  "lb_strategy": "round_robin",
  "sticky_ttl": 1800
}
```

- 新增字段 `lb_strategy`，枚举值 `round_robin | backup | sticky`，默认 `round_robin`
- 新增字段 `sticky_ttl`，会话粘性有效期（秒），默认 1800，仅 `sticky` 策略时生效
- 现有 `max_fail_count` 和 `cooldown_seconds` 对所有策略生效（健康检查是独立层）

### config.py

- `_CONFIG_SCHEMA` 新增 `lb_strategy`：`{"type": "str", "default": "round_robin", "requires_restart": False}`
- `_CONFIG_SCHEMA` 新增 `sticky_ttl`：`{"type": "int", "default": 1800, "requires_restart": False}`
- `_VALID_VALUES` 新增 `"lb_strategy": ["round_robin", "backup", "sticky"]`
- `_VALID_VALUES` 新增 `"sticky_ttl": {"min": 60, "max": 86400}`
- `_apply_lb_settings()` 热更新 `load_balancer.update_strategy()` 和 `sticky_ttl`

## select_channel 接口变更

```python
async def select_channel(
    self,
    channels: list[Channel],
    exclude_ids: set[str] | None = None,
    client_ip: str | None = None,     # 新增：源 IP
    api_key: str | None = None,       # 新增：客户端 API key（已解析的明文 key 或 key 名）
    extra_headers: dict[str, str] | None = None,  # 新增：客户端原始 headers（用于 sticky 策略提取会话标识）
) -> Optional[Channel]:
```

### proxy_core.py 调用变更

`proxy_core.py` 中所有调用 `select_channel` 的地方需传入上下文：

- `client_ip`：从 `CombinedMiddleware` 写入 `scope["state"]["client_ip"]` 获取
- `api_key`：从 `scope["state"]["api_key_id"]` 获取
- `extra_headers`：从请求 headers 中提取（过滤掉 `authorization`、`x-api-key` 等敏感头，或只传 sticky 策略需要的特定 headers）

调用点（共 2 处主调用）：
- `proxy_core.py:769` — 模型组 fallback 循环中的渠道选择
- `proxy_core.py:827` — 单模型请求的渠道选择

## 策略实现

### round_robin（现有行为，无变化）

`select_channel` 现有逻辑：
1. 过滤禁用/不健康/excluded 渠道
2. 按 priority 分组，取最高优先级组
3. 组内 SWRR 选择

### backup（顺序备份）

```
1. 过滤禁用/不健康/excluded 渠道
2. 按 priority 升序排序
3. priority 相同时按 weight 降序排序（权重高的排前面）
4. 返回第一个渠道（即最靠前的健康渠道）
```

`weight` 在 backup 策略下仅用于同优先级内的排序优先级，不做流量分配。

### sticky（会话粘性 + 一致性哈希）

#### 第一步：构建复合会话标识

按优先级获取，命中即停止：

**优先级 1 — 显式 session header（直接作为键）：**
- `X-Session-ID`（自定义 header）
- `x-claude-code-session-id`（Claude Code 已有）

任一存在 → 直接使用其值作为哈希键。

**优先级 2 — 复合键（全量拼接，保证非空）：**

将以下所有部分用 `|` 拼接，不存在的 header 置为 `None`：

| 组成部分 | 来源 | 缺失时 |
|----------|------|--------|
| Authorization | `Authorization` header | `"None"` |
| X-API-Key | `x-api-key` 或 `X-API-Key` header | `"None"` |
| User-Agent | `User-Agent` header | `"None"` |
| 源 IP | `client_ip` 参数 | `"None"` |

拼接格式：`{Authorization}|{x_api_key}|{User-Agent}|{client_ip}`

所有 header 都不存在时，键为 `None|None|None|{client_ip}`，仍保证非空。

#### 第二步：一致性哈希选渠道

```python
def _consistent_hash_select(
    self,
    key: str,
    available: list[Channel],
) -> Channel:
```

**哈希环构造：**
- 每个渠道分配 `weight * replicas`（虚拟节点数，`replicas` 基础值默认 100）个位置
- 权重 5 的渠道 → 500 个虚拟节点；权重 1 → 100 个
- 位置计算：`hash(f"{channel.id}:{i}") % RING_SIZE`
- `RING_SIZE = 65536`
- 哈希函数：`hashlib.md5`（取前 4 字节转 int，足够分布均匀且快速）

**选渠道：**
1. 计算 `key_hash = hash(key) % RING_SIZE`
2. 在环上顺时针找到第一个匹配的渠道
3. 如果该渠道不可用（在 `available` 中不存在），继续顺时针找下一个

**渠道变化时的行为：**
- 新增/删除渠道 → 环重新构建 → 只有映射到该渠道的会话（约 1/N）需要重新分配
- 不可用渠道（unhealthy/excluded）→ 顺时针 fallback 到下一个健康渠道

#### 第三步：会话活跃度缓存（仅 sticky 策略）

sticky 策略维护一个内存缓存，记录每个会话的最后活跃时间和绑定渠道：

```python
_sticky_cache: dict[str, tuple[str, float]]  # key → (channel_id, last_active_ts)
```

**粘性判断逻辑：**
1. 构建会话标识 `session_key`
2. 查 `_sticky_cache[session_key]`
3. 若存在且 `now - last_active < sticky_ttl` → 返回缓存的渠道（若该渠道仍可用）
4. 若过期或不存在 → 一致性哈希分配新渠道，写入缓存

**清理机制：**
- 每次读写时顺带清理过期条目（`last_active + sticky_ttl < now`）
- 无需后台定时任务

**配置项：**
- `sticky_ttl`：会话粘性有效期（秒），默认 1800（30 分钟），在设置页可配置
- 仅 `lb_strategy=sticky` 时生效，其他策略不维护此缓存

## 优先级和权重字段的语义变化

| 策略 | `priority` 含义 | `weight` 含义 |
|------|----------------|---------------|
| `round_robin` | 分组层级（不变） | 同级内流量比例（不变） |
| `backup` | 尝试顺序（数字越小越先） | 同级内排序权重（高权重排前面，不做流量分配） |
| `sticky` | 健康回退时的尝试顺序 | 控制哈希环上虚拟节点数量（权重越大，节点越多，映射会话比例越大） |

## UI 变更

### 设置页（settings.html）负载均衡 TAB

在现有「失败次数阈值」和「冷却时间」上方新增：

1. **负载均衡策略** — `<select>` 下拉框：
   - `round_robin` → "加权轮询（默认）"
   - `backup` → "顺序备份"
   - `sticky` → "会话粘性"
   - 带描述文字说明每种策略的行为

2. 策略下方的说明文字动态更新：
   - `round_robin` → "同优先级内按权重轮询分发流量，低优先级渠道作为备份。"
   - `backup` → "按优先级顺序逐个尝试，始终使用最靠前的健康渠道，失败后才切换到下一个。"
   - `sticky` → "同一会话的请求始终路由到同一渠道，利用上游缓存。会话标识自动从 X-Session-ID / x-claude-code-session-id / Authorization + x-api-key + User-Agent + IP 构建。"

3. **粘性有效期** — 仅在策略为 `sticky` 时显示：
   - 数字输入框，默认 1800，单位秒
   - 说明："超过此时长无请求的会话将重新分配渠道。建议与上游缓存 TTL 对齐。"

### 渠道管理页

无需变更。`priority` 和 `weight` 字段已存在，只是在不同策略下语义略有不同（见上表）。

## 文件变更清单

| 文件 | 变更 |
|------|------|
| `config.py` | `_CONFIG_SCHEMA` 新增 `lb_strategy`、`sticky_ttl`；`_VALID_VALUES` 新增枚举和范围；`_apply_lb_settings()` 传递策略和 TTL |
| `balancer/load_balancer.py` | 新增策略枚举；`select_channel` 按策略分支；新增 `_backup_select`、`_sticky_select`、`_consistent_hash_select`、`_build_session_key` 方法；新增 `_sticky_cache` 活跃度缓存和清理逻辑 |
| `proxy_core.py` | `select_channel` 调用处传入 `client_ip`、`api_key`、`extra_headers` |
| `main.py` | `CombinedMiddleware` 中将 `client_ip` 和必要 headers 写入 `scope["state"]` |
| `static/fragments/admin/settings.html` | 负载均衡 TAB 新增策略下拉框和动态说明 |
| `static/index.html`（或对应管理页面） | 无需变更（priority/weight 字段已存在） |
| `tests/` | 新增 backup/sticky 策略的单元测试 |

## 测试要点

1. **round_robin** — 现有测试不变
2. **backup** — 同优先级内按 weight 排序；全部失败时 fallback 到低优先级
3. **sticky** — 同一 session key 始终映射到同一渠道；渠道不可用时 fallback 到下一个健康渠道；一致性哈希的渠道增减稳定性；`sticky_ttl` 过期后重新分配渠道；过期条目自动清理
4. **策略切换** — 设置页切换策略后热生效，无需重启
5. **边界情况** — 所有渠道不可用时返回 None；无 session header 且无 API key 时复合键仅含 IP，仍正常工作
