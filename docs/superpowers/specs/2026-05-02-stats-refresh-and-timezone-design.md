# 统计页面"刷新统计"按钮改造 & 时区统一

## 背景

统计页面（`/static/index.html` 的"统计"Tab）中"刷新统计"按钮存在以下问题：

1. **语义不对** — 按钮调用 `POST /admin/stats/refresh/daily`，实际做的是"补全缺失的日聚合数据"，不是用户期望的"刷新统计数据"
2. **反馈糟糕** — 点击后 `alert` 弹出一大堆调试信息（服务器日期、缺失日期、requests 表范围等），对普通用户毫无意义
3. **不刷新当天** — `refresh_missing_daily_stats()` 故意排除当天，用户无法看到当天的最新聚合
4. **时区不一致** — 所有 SQL 和 Python 时间计算使用 UTC/系统时区，中国用户看到的"今天"和"近 N 天"边界与预期不符

## 目标

1. 点击"刷新统计"：补全缺失历史聚合 + 强制刷新近 3 天（含当天）的日聚合 + 刷新近 24 小时的时聚合
2. 全部统计统一使用东 8 区（UTC+8）时区，硬编码
3. 刷新后行内提示，无 alert 弹窗

## 设计

### 1. 时区处理（后端）

**核心思路：** PostgreSQL 存储 UTC 时间，所有"东 8 区今天"和"近 N 天"的判断统一通过 SQL `now() + interval '8 hours'` 偏移计算。后端返回给前端的时间字符串已经是东 8 区时间，前端直接显示。

**新增工具函数 `stats.py`：**

```python
def utc8_now() -> datetime:
    """返回当前东8区时间"""
    return datetime.utcnow() + timedelta(hours=8)
```

**受影响的函数及修改：**

| 函数 | 当前问题 | 修改内容 |
|------|---------|---------|
| `get_overall_stats()` | `now() - N days` 用 UTC 划界 | SQL 中 `now()` 改为 `now() + '8h'::interval` |
| `get_daily_stats()` | `date.today()` 是 Python 系统时区 | 用 `utc8_now().date()` 计算 start_date |
| `get_daily_stats_from_requests()` | 同上 | 同上 |
| `get_hourly_stats_from_requests()` | start_time 由调用方传入 | 调用方（admin.py）改用 utc8_now() 计算 start_hour |
| `refresh_missing_daily_stats()` | `date.today()` 用 UTC/系统时区 | 改用 `utc8_now().date()` |
| `aggregate_daily_stats()` | SQL 中 `date_trunc('day', timestamp)` 用 UTC 划日 | 改为 `date_trunc('day', timestamp + interval '8 hours')` |
| `aggregate_hourly_stats()` | SQL 中 `date_trunc('hour', timestamp)` 用 UTC 划时 | 改为 `date_trunc('hour', timestamp + interval '8 hours')`，保证小时边界对齐东 8 区 |
| admin.py `get_stats()` | `datetime.now()` 计算 start_hour | 改用 `utc8_now()` |

**SQL 时区偏移模式：**

所有涉及"近 N 天"的 `WHERE` 条件，从：
```sql
WHERE timestamp >= now() - ($1 || ' days')::interval
```
改为：
```sql
WHERE timestamp >= (now() + interval '8 hours') - ($1 || ' days')::interval
```

所有涉及"按日分组"的 `date_trunc('day', timestamp)` 改为 `date_trunc('day', timestamp + interval '8 hours')`，确保跨日边界对齐东 8 区。

### 2. 刷新按钮逻辑（后端）

**新增端点：** `POST /admin/stats/refresh`

**一次调用完成三步：**

1. **补全缺失历史聚合** — 找出 `daily_stats` 中缺失的历史日期（排除近 3 天，不含当天），按连续区间分组调用 `aggregate_daily_stats`
2. **强制刷新近 3 天日聚合** — 对东 8 区的"今天"及前 2 天，调用 `aggregate_daily_stats`（`ON CONFLICT DO UPDATE` 会覆盖旧数据）
3. **强制刷新近 24 小时时聚合** — 对近 24 小时调用 `aggregate_hourly_stats`

**返回值：**

```json
{
  "backfilled_count": 5,
  "recent_refreshed_days": 3,
  "hourly_refreshed": true
}
```

不包含 `debug` 字段。

**实现位置：** 在 `stats.py` 中新增 `refresh_stats()` 函数，在 `routers/admin.py` 中新增端点调用它。

**旧端点 `POST /admin/stats/refresh/daily` 保留不删**，避免破坏其他调用方，但前端不再使用。

### 3. 前端改造

**按钮行为流程：**

1. 用户点击"刷新统计"
2. 按钮文字变为"刷新中..."，按钮禁用
3. 调用 `POST /admin/stats/refresh`
4. 完成后按钮文字恢复，按钮重新启用
5. 按钮旁显示绿色"已刷新"行内提示，1.5 秒后淡出
6. 自动调用 `loadStats()` 重新加载页面数据

**去掉 alert：** 完全移除 `refreshDailyStats()` 中的 `alert(msg)` 及所有调试信息拼接。

**行内提示实现：** 在按钮旁边添加 `<span id="refreshHint">` 元素，默认隐藏。刷新完成后设置 `textContent = '已刷新'` + 绿色样式类，1.5 秒后通过 `setTimeout` 清除文字并隐藏。

**函数重命名：** `refreshDailyStats()` → `refreshStats()`，调用新端点。

### 4. 测试

**后端测试（`tests/test_stats_pg.py`）：**

- 新增测试 `test_refresh_stats`：写入多天请求记录（含当天），调用 `refresh_stats()`，验证 `daily_stats` 和 `hourly_stats` 均正确聚合
- 新增测试 `test_timezone_offset`：写入 UTC 时间在东 8 区跨日边界附近的请求记录，验证聚合按东 8 区日期正确归类

**前端：** 手动验证按钮行为正确、行内提示正常、无 alert 弹窗。

## 不做的事

- 不自动定时聚合（用户手动点击刷新即可）
- 不在前端做时区偏移（后端统一返回东 8 区时间）
- 不删除旧的 `/admin/stats/refresh/daily` 端点
- 不做时区配置化（硬编码 UTC+8）
