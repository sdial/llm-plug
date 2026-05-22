# Admin IP 白名单设计文档

**日期**：2026-05-22  
**状态**：已审批，待实现

## 背景

`/admin/*` 路由目前完全无鉴权，任何能访问 55555 端口的请求都可管理渠道、API Key、查看日志。  
目标是先用 IP 白名单保护管理后台，后续再叠加用户名密码登录。

## 目标

- 基于规则的 IP 白名单，支持路径 glob、HTTP 方法、IP/CIDR 三维匹配
- 规则存储在 `data/whitelist.csv`，热重载，无需重启
- 管理后台提供 textarea 编辑 + 保存校验界面，界面即文档
- 403 时返回人类可读的拒绝原因

## 数据格式

### `data/whitelist.csv`

```csv
# 注释行以 # 开头，会被忽略
# 空行也会被忽略
path_pattern,methods,ip_cidr,description
/admin/*,*,10.1.1.0/24,家庭内网
/admin/*,*,127.0.0.1,本机
/admin/stats,GET,203.0.113.5,公司只读
```

**列说明**：
| 列 | 格式 | 说明 |
|----|------|------|
| `path_pattern` | glob（`fnmatch`） | `/admin/*` 匹配所有管理接口 |
| `methods` | `*` 或 `GET\|POST\|PUT\|DELETE` | `*` 表示所有方法 |
| `ip_cidr` | IPv4/IPv6 精确地址或 CIDR | `10.0.0.0/8`、`192.168.1.1` |
| `description` | 任意文本 | 用于说明，也出现在 403 信息中 |

**匹配语义**：每行规则内部是 AND（路径 AND 方法 AND IP 同时满足才算命中），多行之间是 OR（命中任意一行即放行）。

## 生效规则

| 场景 | 行为 |
|------|------|
| CSV 不存在或无有效规则 | 放行所有（向后兼容） |
| 请求路径不在任何规则的 `path_pattern` 范围内 | 放行 |
| 路径匹配，但 IP 不在任何匹配规则的 CIDR 中 | 403，原因：`不在 IP 白名单范围内` |
| 路径匹配，IP 匹配，但方法不允许 | 403，原因：`该 IP 不允许使用 {METHOD} 方法` |

## 模块设计

### 新增 `whitelist.py`

```
WhitelistRule(dataclass)
  path_pattern: str
  methods: set[str]   # 空集合 = 全部
  network: IPv4Network | IPv6Network
  description: str

WhitelistCache
  _path: str
  _mtime: float
  _rules: list[WhitelistRule]

  get_rules() -> list[WhitelistRule]
    # os.stat() 检查 mtime，变化时重载，开销 < 0.1ms/请求

load_rules(path) -> list[WhitelistRule]
  # 解析 CSV，跳过 # 开头和空行

check_request(rules, path, method, client_ip) -> tuple[bool, str]
  # 返回 (放行, 拒绝原因)
  # 纯函数，便于单元测试
```

### `CombinedMiddleware` 改动（`main.py`）

在现有逻辑最前面插入白名单检查：

```
1. 获取 client_ip（REMOTE_ADDR；若启用 trusted_proxy 则读 X-Forwarded-For 首项）
2. 调用 whitelist_cache.get_rules()
3. 若规则非空：调用 check_request()，不通过则返回 403 JSON
4. 继续现有 proxy auth 流程（仅对代理路径）
```

**403 响应格式**（与现有错误格式一致）：
```json
{"error": {"message": "不在 IP 白名单范围内", "type": "ip_whitelist_error"}}
```

### 新增 Admin API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/admin/whitelist` | 返回 CSV 原始文本（含注释） |
| `PUT` | `/admin/whitelist` | 写入前服务端二次校验，返回解析后规则数 |

### Admin UI（`static/index.html`）

在设置页新增「IP 白名单」标签页，包含：

**说明区**（始终可见，界面即文档）：
> 每行一条规则，4 列用逗号分隔：路径模式、允许方法、IP 或网段、备注说明。  
> `#` 开头的行为注释，空行忽略。  
> 规则之间是「或」关系，同一行内路径、方法、IP 三者同时满足才放行。  
> 文件为空或无规则时，不做任何限制。

**格式示例**（代码块展示）：
```
path_pattern,methods,ip_cidr,description
/admin/*,*,10.1.1.0/24,家庭内网
/admin/*,*,127.0.0.1,本机
/admin/stats,GET,203.0.113.5,公司只读
```

**字段速查表**：
| 列 | 示例 | 说明 |
|----|------|------|
| path_pattern | `/admin/*` | 支持 `*` 通配，`/admin/*` 匹配所有管理接口 |
| methods | `*` 或 `GET\|POST` | `*` 表示不限方法 |
| ip_cidr | `192.168.1.0/24` | 支持精确 IP 和 CIDR 网段 |
| description | `家庭内网` | 备注，也会出现在 403 错误信息中 |

**编辑区**：
- `<textarea>` 展示完整 CSV 原文（含注释）
- 「保存」前前端校验：非空行非注释行必须恰好 4 列，ip_cidr 列格式粗检
- 后端保存失败时展示详细错误

## 不在范围内

- IP 信任代理配置（`trusted_proxy`）暂不实现，留到公网部署阶段
- 白名单规则的热编辑不支持实时预览当前 IP（可后续加）
- 不支持 IPv6 CIDR 的前端格式校验（后端支持，前端仅提示）
