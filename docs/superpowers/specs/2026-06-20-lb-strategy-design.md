# 负载均衡策略扩展设计

## 概述

为负载均衡模块引入可选全局策略，替代当前硬编码的加权轮询算法。用户在设置页「负载均衡」Tab 通过下拉框选择策略。

设计目标：

- 保持现有 `round_robin` 行为完全兼容，默认值不改变线上分流。
- 让 `priority` 在所有策略中都表示「优先级分层」：数字越小越优先，低优先级只作为备份。
- 让 `weight` 在不同策略中含义明确，不混用「排序」和「流量比例」。
- `sticky` 策略不得把原始 `Authorization`、`x-api-key` 等敏感值留在缓存 key 或日志中。
- 所有路由决策使用确定性哈希，跨进程、跨重启保持稳定。

## 策略定义

| 配置值 | 策略名 | 行为 |
|--------|--------|------|
| `round_robin` | 加权轮询 | 当前默认行为。同优先级内按权重 SWRR 分发，低优先级作为备份。 |
| `backup` | 顺序备份 | 始终选择最高优先级组内排序最靠前的健康渠道；失败后才切换到同组下一个或低优先级组。 |
| `sticky` | 会话粘性 | 按会话标识在最高优先级健康组内做加权稳定哈希，同一会话优先命中同一上游。 |

## 配置

### settings.json

```json
{
  "lb_strategy": "round_robin",
  "sticky_ttl": 1800,
  "sticky_cache_max_entries": 10000
}
```

- 新增字段 `lb_strategy`，枚举值为 `round_robin | backup | sticky`，默认 `round_robin`。
- 新增字段 `sticky_ttl`，会话粘性有效期，单位为秒，默认 1800。仅 `sticky` 策略生效。
- 新增字段 `sticky_cache_max_entries`，sticky 缓存最大条目数，默认 10000。仅 `sticky` 策略生效。
- 现有 `max_fail_count` 和 `cooldown_seconds` 对所有策略生效，健康检查仍是独立层。

### config.py

- `_CONFIG_SCHEMA` 新增 `lb_strategy`：`{"type": "str", "default": "round_robin", "requires_restart": False}`。
- `_CONFIG_SCHEMA` 新增 `sticky_ttl`：`{"type": "int", "default": 1800, "requires_restart": False}`。
- `_CONFIG_SCHEMA` 新增 `sticky_cache_max_entries`：`{"type": "int", "default": 10000, "requires_restart": False}`。
- `_CONFIG_CONSTRAINTS` 新增 `lb_strategy` choices：`("round_robin", "backup", "sticky")`。
- `_CONFIG_CONSTRAINTS` 新增 `sticky_ttl` 范围：`min=60, max=86400`。
- `_CONFIG_CONSTRAINTS` 新增 `sticky_cache_max_entries` 范围：`min=100, max=1000000`。
- `_apply_lb_settings()` 调用 `load_balancer.update_config(...)`，一次性传入失败阈值、冷却时间、策略、sticky TTL 和 sticky 缓存容量。
- 当 `lb_strategy`、`sticky_ttl`、`sticky_cache_max_entries` 或 sticky key 构造规则变化时，必须清空 `_sticky_cache`。

## select_channel 接口变更

```python
async def select_channel(
    self,
    channels: list[Channel],
    exclude_ids: set[str] | None = None,
    client_ip: str | None = None,
    api_key_id: str | None = None,
    client_headers: dict[str, str] | None = None,
) -> Optional[Channel]:
```

参数说明：

| 参数 | 说明 |
|------|------|
| `channels` | 当前模型已过滤出的候选渠道。 |
| `exclude_ids` | 本次请求已尝试失败的渠道 ID 集合。 |
| `client_ip` | 客户端 IP，用于没有显式 session 时补充 sticky key。 |
| `api_key_id` | 已解析的客户端 API Key 名称或 ID，不传入明文 key。 |
| `client_headers` | 客户端请求头，仅用于提取非敏感会话特征；不得将原始敏感值保存进缓存。 |

### proxy_core.py 调用变更

`proxy_core.py` 中所有调用 `select_channel` 的地方都传入请求上下文：

- `client_ip`：使用已传入 `proxy_request()` 的 `client_ip` 参数。
- `api_key_id`：使用已传入 `proxy_request()` 的 `api_key_id` 参数。
- `client_headers`：使用已传入 `_do_request()` 的客户端 headers 同源数据，但只供 `LoadBalancer` 提取 sticky 特征。

调用点：

- `proxy_core.py` 单模型请求循环中的渠道选择。
- `proxy_core.py` 模型组 fallback 循环中的渠道选择。

`CombinedMiddleware` 需要在 `scope["state"]` 中写入 `client_ip` 和认证后的 `api_key_id`。如果当前请求未启用客户端 API Key，`api_key_id` 为空字符串或 `None`。

## 通用选择流程

所有策略共享同一层过滤和分组逻辑：

1. 过滤 `enabled=False`、`exclude_ids` 命中、健康检查不通过的渠道。
2. 如果没有可用渠道，返回 `None`。
3. 按 `priority` 升序分组。
4. 只在当前最高优先级健康组内执行策略选择。
5. 当前优先级组内所有渠道都被本次请求尝试失败后，下一轮选择才进入低优先级组。

该约束是硬规则：`sticky` 不得在所有 `available` 渠道上直接哈希，否则会破坏低优先级作为备份的语义。

## 策略实现

### round_robin

保持现有行为不变：

1. 过滤不可用渠道。
2. 取最高优先级健康组。
3. 组内使用平滑加权轮询（SWRR）。

`weight` 表示同优先级组内的目标流量比例。

### backup

选择流程：

1. 过滤不可用渠道。
2. 取最高优先级健康组。
3. 组内按 `weight` 降序排序。
4. `weight` 相同时按 `id` 升序稳定排序。
5. 返回排序后的第一个渠道。

`backup` 策略下，`weight` 是同优先级组内的稳定排序权重，不做流量分配。若同组第一渠道失败，`exclude_ids` 会让下一轮选择同组下一个渠道；同组全部失败后才进入低优先级组。

### sticky

`sticky` 目标是让同一会话尽量命中同一上游，用于复用上游缓存。它不是强一致绑定：渠道不可用、被本次请求排除、配置变化或缓存过期时可以重新分配。

#### 会话 key 构造

会话 key 分两级构造。

**第 1 级：显式 session header**

按以下顺序查找，命中后停止：

1. `x-session-id`
2. `x-claude-code-session-id`

显式 session header 的值视为用户提供的会话标识。实现时必须先做长度限制和脱敏哈希，不得把原始值作为 `_sticky_cache` key。

**第 2 级：结构化复合标识**

没有显式 session header 时，使用结构化对象生成复合标识：

```python
parts = {
    "api_key_id": api_key_id or "",
    "user_agent": client_headers.get("user-agent", "") if client_headers else "",
    "client_ip": client_ip or "",
}
```

设计约束：

- 不使用原始 `Authorization` 或 `x-api-key`。
- 不使用 `"None"` 这类可与真实输入碰撞的字符串占位符。
- 不用字符串拼接表达结构，使用 `json.dumps(parts, sort_keys=True, separators=(",", ":"))` 得到 canonical JSON。
- `client_headers` 必须在进入构造前统一转成小写 key。

最终缓存 key 使用确定性摘要：

```python
session_fingerprint = hashlib.sha256(canonical_session_identity.encode("utf-8")).hexdigest()
```

如果项目后续引入进程级 secret，可以升级为 `hmac.new(secret, canonical, hashlib.sha256).hexdigest()`。当前 SPEC 不强制引入新 secret，避免扩大配置面。

#### 加权稳定哈希

`sticky` 使用加权 Rendezvous Hashing（Highest Random Weight, HRW），不使用固定大小哈希环。

原因：

- 不依赖 Python `hash()`，跨进程稳定。
- 无 `RING_SIZE` 碰撞和虚拟节点调参问题。
- 渠道增减时只影响少量会话映射。
- 渠道数量通常较小，扫描当前优先级组的 O(n) 成本可接受。

选择算法：

```python
def _sticky_select_by_hrw(session_key: str, candidates: list[Channel]) -> Channel:
    best_channel = None
    best_score = None
    for channel in candidates:
        digest = hashlib.sha256(f"{session_key}:{channel.id}".encode("utf-8")).digest()
        value = int.from_bytes(digest[:8], "big") / 2**64
        value = max(value, 1e-12)
        score = -math.log(value) / max(channel.weight, 1)
        if best_score is None or score < best_score:
            best_score = score
            best_channel = channel
    return best_channel
```

说明：

- 使用 `hashlib.sha256`，不使用 Python 内置 `hash()`。
- `weight` 越大，命中概率越高。
- `weight <= 0` 不应出现在合法 `Channel` 中；实现中仍使用 `max(channel.weight, 1)` 防御。
- 只在最高优先级健康组内计算，不跨优先级组混选。

#### sticky 缓存

缓存记录：

```python
_sticky_cache: OrderedDict[str, StickyCacheEntry]

@dataclass
class StickyCacheEntry:
    channel_id: str
    last_active_at: float
```

缓存规则：

1. 构造 `session_fingerprint`。
2. 查询 `_sticky_cache`。
3. 若条目存在、未过期，且 `channel_id` 仍在当前最高优先级健康组内，则返回该渠道并刷新 `last_active_at`。
4. 若条目不存在、过期、渠道不可用、渠道被 `exclude_ids` 排除，或该渠道不在当前最高优先级组内，则重新通过 HRW 选择。
5. 新选择结果写入 `_sticky_cache`，并移动到 LRU 尾部。

清理规则：

- 每次读写最多清理一批过期条目，避免单次请求 O(n) 扫描全部缓存。
- 当缓存超过 `sticky_cache_max_entries` 时，按 LRU 淘汰最久未访问条目。
- 策略从 `sticky` 切到其他策略时清空缓存。
- `sticky_ttl` 或 `sticky_cache_max_entries` 变化时清空缓存，避免旧 TTL 语义残留。

并发规则：

- `_health`、SWRR 的 `current_weight`、`_strategy`、`_sticky_cache` 的读写必须在同一个 `asyncio.Lock` 保护下完成。
- 不在锁内执行网络请求。
- `select_channel()` 内部只做纯内存计算，锁粒度保持当前实现级别。

## 优先级和权重语义

| 策略 | `priority` 含义 | `weight` 含义 |
|------|----------------|---------------|
| `round_robin` | 分组层级，数字越小越优先 | 同级内 SWRR 流量比例 |
| `backup` | 分组层级，数字越小越优先 | 同级内排序权重，越大越靠前 |
| `sticky` | 分组层级，数字越小越优先 | 同级内 HRW 命中概率权重 |

不允许任何策略绕过 `priority` 分组直接在所有健康渠道中选择。

## 安全与隐私约束

- `_sticky_cache` key 只能是 SHA-256 或 HMAC-SHA256 摘要。
- 不在缓存、日志、异常信息或测试快照中保存原始 `Authorization`、`x-api-key`、显式 session header 值。
- `client_headers` 只作为输入特征读取，`LoadBalancer` 不持久化原始 headers。
- session header 值建议限制最大长度，例如 512 字符。超长值截断或摘要后再参与 canonical JSON。
- API Key 已认证时优先使用 `api_key_id`。未认证开放代理模式下，`api_key_id` 为空，由 `user_agent + client_ip` 提供低强度粘性。

## UI 变更

### 设置页负载均衡 Tab

在现有「失败次数阈值」和「冷却时间」上方新增：

1. **负载均衡策略**
   - `round_robin`：加权轮询（默认）
   - `backup`：顺序备份
   - `sticky`：会话粘性

2. **策略说明**
   - `round_robin`：同优先级内按权重轮询分发流量，低优先级渠道作为备份。
   - `backup`：按优先级和权重排序，始终使用最靠前的健康渠道，失败后才切换。
   - `sticky`：同一会话优先路由到同一渠道，用于复用上游缓存；会话标识会先脱敏再参与路由。

3. **粘性有效期**
   - 仅 `sticky` 策略显示。
   - 数字输入框，默认 1800，单位秒。
   - 说明：超过此时长无请求的会话会重新分配渠道，建议与上游缓存 TTL 对齐。

4. **粘性缓存容量**
   - 仅 `sticky` 策略显示。
   - 数字输入框，默认 10000。
   - 说明：达到上限后按 LRU 淘汰最久未访问的会话。

### 渠道管理页

无需新增字段。`priority` 和 `weight` 已存在，但说明文案应补充不同策略下的语义差异，避免用户误以为 `backup` 下的 `weight` 仍表示流量比例。

## 文件变更清单

| 文件 | 变更 |
|------|------|
| `config.py` | 新增 `lb_strategy`、`sticky_ttl`、`sticky_cache_max_entries` 配置及校验；`_apply_lb_settings()` 传递完整 LB 配置。 |
| `balancer/load_balancer.py` | 新增策略枚举、统一过滤分组、backup 选择、sticky key 构造、HRW 选择、LRU TTL 缓存和配置热更新。 |
| `proxy_core.py` | `select_channel()` 调用处传入 `client_ip`、`api_key_id`、`client_headers`。 |
| `main.py` | `CombinedMiddleware` 写入 `scope["state"]["client_ip"]` 和认证后的 `api_key_id`；确保请求 headers 可安全传递到代理层。 |
| `static/fragments/admin/settings.html` | 负载均衡 Tab 新增策略、sticky TTL、sticky 缓存容量控件和动态说明。 |
| `tests/` | 新增策略、sticky 安全、配置热更新和代理调用上下文测试。 |

## 测试要点

1. **兼容性**
   - 默认 `lb_strategy=round_robin`。
   - 现有 SWRR 测试不需要修改预期。
   - 旧 `max_fail_count`、`cooldown_seconds` 仍热生效。

2. **backup**
   - 只选择最高优先级健康组。
   - 同优先级内按 `weight desc, id asc` 选择。
   - 第一渠道失败后，本次请求通过 `exclude_ids` 切到同组下一个。
   - 同组全部失败后才进入低优先级组。

3. **sticky key**
   - `x-session-id` 优先于 `x-claude-code-session-id`。
   - 没有显式 session 时使用 `api_key_id + user_agent + client_ip`。
   - 不保存原始 `Authorization`、`x-api-key` 或 session header。
   - `"None"`、空字符串、包含 `|` 的 header 值不会造成结构碰撞。

4. **sticky 选择**
   - 同一 session 在同一最高优先级健康组内稳定命中同一渠道。
   - 不同 `weight` 影响大量 session 的统计分布。
   - 高优先级组存在健康渠道时，sticky 不会选择低优先级渠道。
   - 绑定渠道不可用或进入 `exclude_ids` 时重新选择。
   - 渠道恢复健康后，新 session 可重新分配到该渠道。

5. **sticky 缓存**
   - TTL 过期后重新分配并刷新缓存。
   - 超过 `sticky_cache_max_entries` 后按 LRU 淘汰。
   - 切换策略、修改 TTL、修改容量时清空缓存。
   - 清理过期条目不会在单次请求中 O(n) 扫描全部缓存。

6. **并发**
   - 并发 `select_channel()` 不破坏 SWRR 权重状态。
   - 并发 sticky 请求不产生缓存结构异常。
   - `update_config()` 与 `select_channel()` 并发时保持一致锁保护。

7. **边界情况**
   - 所有渠道不可用时返回 `None`。
   - `channels=[]` 返回 `None`。
   - 未配置客户端 API Key 的开放代理模式下 sticky 仍可工作，但只提供基于 `user_agent + client_ip` 的弱粘性。
