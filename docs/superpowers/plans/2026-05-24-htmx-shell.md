# index.html 壳 + htmx 渐进改造 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将管理页改造成轻量页面壳，静态资源外置，并为局部 htmx 片段刷新建立第一批路由。

**架构：** `static/index.html` 只保留导航、主容器和脚本引用。新增 `htmx.min.js` 到 `static/js/`，同时新增少量 `/admin/ui/*` 片段路由，先覆盖最适合服务端片段化的区域，保留复杂交互的现有模块化 JS。

**技术栈：** FastAPI、htmx 2.x、本地静态文件、原生 HTML/CSS/JS

---

### 任务 1：引入本地 htmx

**文件：**
- 创建：`static/js/htmx.min.js`
- 修改：`static/index.html`
- 测试：`tests/test_static_admin_split.py`

- [ ] **步骤 1：下载官方 htmx 2.0.9 到本地静态目录**

```powershell
Invoke-WebRequest -Uri "https://cdn.jsdelivr.net/npm/htmx.org@2.0.9/dist/htmx.min.js" -OutFile "static/js/htmx.min.js"
```

- [ ] **步骤 2：让 index.html 引用本地 htmx**

```html
<script src="/static/js/htmx.min.js"></script>
```

- [ ] **步骤 3：验证文件存在且页面引用正确**

运行：`uv run pytest tests/test_static_admin_split.py -q`
预期：PASS

### 任务 2：把 index.html 收缩成页面壳

**文件：**
- 修改：`static/index.html`
- 创建：`routers/admin_ui.py`
- 修改：`main.py`
- 测试：`tests/test_admin_ui_fragments.py`

- [ ] **步骤 1：编写片段路由测试**

```python
def test_admin_ui_fragments_return_html(client):
    resp = client.get("/admin/ui/channels")
    assert resp.status_code == 200
    assert "hx-get" in resp.text
```

- [ ] **步骤 2：实现片段路由**

```python
@router.get("/ui/channels")
async def channels_fragment():
    return HTMLResponse("<section id='channels-panel' hx-get='/admin/ui/channels' hx-trigger='load'>...</section>")
```

- [ ] **步骤 3：让主页壳只保留 htmx 容器和导航**

```html
<div id="admin-content" hx-get="/admin/ui/channels" hx-trigger="load"></div>
```

- [ ] **步骤 4：验证片段路由和主页壳都可加载**

运行：`uv run pytest tests/test_admin_ui_fragments.py -q`
预期：PASS

