import re
from pathlib import Path

STATIC_DIR = Path("static")
INDEX_HTML = STATIC_DIR / "index.html"
LOGIN_HTML = STATIC_DIR / "admin-login.html"


def test_admin_assets_are_split_into_cohesive_modules():
    html = INDEX_HTML.read_text(encoding="utf-8")

    expected_assets = [
        "css/admin.css",
        "js/tailwind-config.js",
        "js/tag_input.js",
        "js/modal.js",
        "js/tab_runtime.js",
        "js/channels_table.js",
        "js/channels_modals.js",
        "js/channels_editor.js",
        "js/apikeys.js",
        "js/stats.js",
        "js/requests.js",
        "js/settings.js",
        "js/model_groups.js",
        "js/whitelist.js",
        "js/storage.js",
        "js/context_shaping.js",
        "js/admin.js",
    ]

    for asset in expected_assets:
        assert (STATIC_DIR / asset).exists()
        assert f"/admin/static/{asset}" in html

    assert 'src="/static/' not in html
    assert 'href="/static/' not in html

    assert not (STATIC_DIR / "js" / "common.js").exists()
    assert "class TagInput" not in html
    assert "async function loadChannels" not in html
    assert "<style>" not in html


def _object_assign_window_keys(js: str) -> set[str]:
    """Extract the keys exported via `Object.assign(window, { ... })`."""
    m = re.search(r"Object\.assign\(window, \{(.*?)\n\}\);", js, re.S)
    return set(re.findall(r"^\s{4}([A-Za-z_$][\w$]*),?\s*$", m.group(1), re.M)) if m else set()


def _direct_window_keys(js: str) -> set[str]:
    """Extract the names assigned via `window.X = ...` (excluding comments)."""
    return {
        m.group(1)
        for line in js.splitlines()
        if not line.lstrip().startswith("//")
        for m in [re.search(r"window\.([A-Za-z_$][\w$]*)\s*=", line)]
        if m
    }


def _window_export_keys(js: str) -> set[str]:
    """Full window export surface: direct assignments + Object.assign keys."""
    return _direct_window_keys(js) | _object_assign_window_keys(js)


def test_channels_monolith_dissolved_and_load_chain_complete():
    """ADR-0020 D1：单体消解——channels.js 不复存在；提供商预设入口下线后，
    加载顺序链 table → modals → editor 完整断言
    （被依赖者先）。防单文件回潮守卫。"""
    assert not (STATIC_DIR / "js" / "channels.js").exists()
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "js/channels.js" not in html

    chain = ["channels_table.js", "channels_modals.js", "channels_editor.js"]
    idx = [html.index(f"/admin/static/js/{name}?v=") for name in chain]
    assert idx == sorted(idx), "加载顺序必须为 table → modals → editor"

    # 基础模块先于全部渠道子域消费方（modal / tab_runtime 顺序各有专项测试）
    modal_idx = html.index("/admin/static/js/modal.js?v=")
    tab_idx = html.index("/admin/static/js/tab_runtime.js?v=")
    assert all(tab_idx < i and modal_idx < i for i in idx)


def test_channels_subdomain_export_surface_minimal():
    """ADR-0020 D1 + D2：全模块导出面总收敛——三个渠道子域模块的
    window 导出集合 = 最小面（index.html / fragments 内联 handler 实际引用 + 跨模块
    门面消费），集合相等断言逐模块守护；每个导出名同步校验有真实消费方。多余导出
    即失败。票 06 后 adminChannels 后门与 closeModal 覆盖均不再存在于渠道子域导出面
    （分别由显式订阅守卫 / 唯一住所守卫单独断言）。"""
    expected_surface = {
        "channels_table.js": {
            "ChannelsTable",  # modals/editor/requests 跨模块门面
            "applyFilters",  # fragments/admin/channels.html 内联 onchange/oninput
        },
        "channels_modals.js": {
            "openTestModal",
            "closeTestModal",
            "toggleStatusWithConfirm",
            "openModelCapModal",
            "closeModelCapModal",
            "saveModelCap",
            "resetModelCap",
        },
        "channels_editor.js": {
            "addEndpointCard",
            "openModal",
            "editChannel",
            "saveChannel",
            "deleteChannelFromModal",
            "fetchModels",
            "closeModelSelectPanel",
            "confirmModelSelect",
            "toggleApiKeyVisibility",
            "applySelectedProfileUrls",
        },
    }

    html = INDEX_HTML.read_text(encoding="utf-8")
    fragment = (STATIC_DIR / "fragments" / "admin" / "channels.html").read_text(encoding="utf-8")
    sources = {name: (STATIC_DIR / "js" / name).read_text(encoding="utf-8") for name in expected_surface}
    requests_js = (STATIC_DIR / "js" / "requests.js").read_text(encoding="utf-8")

    for name, surface in expected_surface.items():
        assert _window_export_keys(sources[name]) == surface, f"{name} 导出面偏离最小面"

    # 参照面 = 外壳/片段内联 handler + 其余渠道子域 + requests.js（经 ChannelsTable
    # 门面消费渠道数据，票 06 起不再经 adminChannels 后门）
    corpora = [html, fragment, requests_js] + [src for other, src in sources.items()]
    for surface in expected_surface.values():
        for exported in surface:
            assert any(exported in corpus for corpus in corpora), f"{exported} 无实际引用却仍导出"


def test_channels_table_subdomain_split_structure():
    """ADR-0020 D1 首刀（票 02）：表格渲染+过滤子域为 channels_table.js。
    结构性存在性守卫：文件存在、被依赖者先加载（modals/editor 均经
    ChannelsTable 门面消费它）——导出面收敛由 test_channels_subdomain_export_surface_minimal
    统一断言。"""
    table_js = (STATIC_DIR / "js" / "channels_table.js").read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "window.ChannelsTable" in table_js
    table_idx = html.index("/admin/static/js/channels_table.js?v=")
    for consumer in ("channels_modals.js", "channels_editor.js"):
        consumer_idx = html.index(f"/admin/static/js/{consumer}?v=")
        assert table_idx < consumer_idx, f"channels_table.js 必须先于 {consumer} 加载"


def test_channels_modal_family_subdomain_split_structure():
    """ADR-0020 D1（票 03）：测试弹窗 / 能力弹窗 / 启停确认三族弹窗子域为
    channels_modals.js。结构性存在性守卫：文件存在、被依赖者先加载、
    ModalManager 门面契约——导出面收敛由 test_channels_subdomain_export_surface_minimal
    统一断言。"""
    modals_js = (STATIC_DIR / "js" / "channels_modals.js").read_text(encoding="utf-8")
    table_js = (STATIC_DIR / "js" / "channels_table.js").read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")

    # 文件 + 加载顺序：channels_modals.js 经 ChannelsTable 门面消费表格子域，被依赖者先加载
    assert "ChannelsTable." in modals_js
    table_idx = html.index("/admin/static/js/channels_table.js?v=")
    modals_idx = html.index("/admin/static/js/channels_modals.js?v=")
    assert table_idx < modals_idx, "channels_table.js 必须先于 channels_modals.js 加载"

    # 弹窗开合全部走 ModalManager 门面，不回退到直接 classList 拨动弹窗 backdrop
    assert "ModalManager.open(" in modals_js
    assert "ModalManager.close(" in modals_js
    assert "ModalManager.confirm(" in modals_js
    assert "testModal').classList" not in modals_js
    assert "modelCapModal').classList" not in modals_js

    # 三族均无跨子域命名空间门面：仅 index.html 内联 onclick + channels_table.js
    # 点击委托两类消费方，扁名全局即最小面（集合断言在导出面守卫测试）
    for name in (
        "openTestModal",
        "closeTestModal",
        "toggleStatusWithConfirm",
        "openModelCapModal",
        "closeModelCapModal",
        "saveModelCap",
        "resetModelCap",
    ):
        assert f"{name}(" in html or f"{name}(" in table_js, f"{name} 无实际引用却仍导出"


def test_channels_editor_subdomain_split_structure():
    """ADR-0020 D1（票 04）：主弹窗 CRUD + 接入点卡片（含模型拉取/选择面板）为
    channels_editor.js。结构性存在性守卫：文件存在、被依赖者先加载、Tab 生命周期
    注册——导出面收敛由 test_channels_subdomain_export_surface_minimal 统一断言。"""
    editor_js = (STATIC_DIR / "js" / "channels_editor.js").read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")

    # 文件 + 加载顺序：editor 经 ChannelsTable 门面消费表格子域
    assert "ChannelsTable." in editor_js
    table_idx = html.index("/admin/static/js/channels_table.js?v=")
    editor_idx = html.index("/admin/static/js/channels_editor.js?v=")
    assert table_idx < editor_idx, "加载顺序必须为 table → editor"

    # 渠道 tab 的生命周期注册唯一住所在编辑器（列表加载 + 表单控件初始化）
    assert editor_js.count("window.TabRuntime.register('channels'") == 1


def test_channels_preset_feature_is_removed():
    """提供商档案已是 Base URL 唯一数据源，不再保留接入点级预设入口。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    editor_js = (STATIC_DIR / "js" / "channels_editor.js").read_text(encoding="utf-8")

    assert not (STATIC_DIR / "js" / "channels_preset.js").exists()
    assert "channels_preset.js" not in html
    assert "presetUrlPanel" not in html
    assert "ep-preset-btn" not in editor_js
    assert "ChannelsPreset" not in editor_js


def test_model_modal_is_the_only_ui_for_input_modality_overrides():
    """图片/音频/文件能力属于渠道模型覆盖，不得回到添加渠道或接入点表单。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    editor_js = (STATIC_DIR / "js" / "channels_editor.js").read_text(encoding="utf-8")
    modals_js = (STATIC_DIR / "js" / "channels_modals.js").read_text(encoding="utf-8")

    for field_id in ("f_cap_image", "f_cap_audio", "f_cap_file"):
        assert f'id="{field_id}"' not in html
        assert field_id not in editor_js
    assert 'id="capImage"' in html
    assert 'id="capAudio"' in html
    assert 'id="capFile"' in html
    assert "JSON.stringify({ model_overrides: modelOverrides })" in modals_js
    assert "endpoints" not in modals_js[modals_js.index("async function saveModelCap") : modals_js.index("async function resetModelCap")]


def test_window_close_modal_single_assignment_site():
    """ADR-0020 D2（票 06）：window.closeModal 唯一住所 = modal.js 兼容层
    （index.html 内联 onclick 依赖全局名）。"""
    sites = [p.name for p in sorted((STATIC_DIR / "js").glob("*.js")) if "window.closeModal =" in p.read_text(encoding="utf-8")]
    assert sites == ["modal.js"], f"closeModal 赋值点回潮: {sites}"

    # 通用门面保留关闭订阅能力，供其他弹窗按需使用
    modal_js = (STATIC_DIR / "js" / "modal.js").read_text(encoding="utf-8")
    assert "onClose: onCloseModal," in modal_js, "ModalManager 门面必须提供 onClose 关闭订阅"


def test_requests_page_consumes_channels_via_explicit_facade():
    """ADR-0020 D2（票 06）：window.adminChannels 全局后门清除——请求页改经
    ChannelsTable 显式接口消费渠道数据（getChannels 读 + onChannelsChanged
    就绪/变更订阅），"渠道为空自动拉取、渠道变更筛选项跟随刷新"行为不变。"""

    def code_lines(js: str) -> list[str]:
        # 只查代码行：注释中的历史说明不构成后门引用
        return [ln for ln in js.splitlines() if not ln.lstrip().startswith(("//", "*", "/*"))]

    for name in ("requests.js", "channels_table.js"):
        js = "\n".join(code_lines((STATIC_DIR / "js" / name).read_text(encoding="utf-8")))
        assert "adminChannels" not in js, f"{name} 仍触碰 adminChannels 后门"

    table_js = (STATIC_DIR / "js" / "channels_table.js").read_text(encoding="utf-8")
    assert "function onChannelsChanged(" in table_js, "渠道数据模块必须导出就绪/变更订阅"
    requests_js = (STATIC_DIR / "js" / "requests.js").read_text(encoding="utf-8")
    assert "ChannelsTable.getChannels()" in requests_js
    assert "window.ChannelsTable?.onChannelsChanged?.(" in requests_js, "请求页必须订阅渠道就绪/变更"


def test_channels_subdomains_fetch_error_boilerplate_eliminated():
    """ADR-0020 D2（票 06）：渠道三子域的手写 fetch 错误三行样板归零——统一走
    admin.js 全局 fetch 包装 + ensureOkResponse 错误提取（401/403/CSRF 重试/
    5xx 提示全局一致）；fetch-models 的 data.error 字段级处理保留局部。"""
    throwing_subdomains = ("channels_editor.js", "channels_modals.js")
    for name in ("channels_table.js",) + throwing_subdomains:
        js = (STATIC_DIR / "js" / name).read_text(encoding="utf-8")
        assert "resp.json().catch" not in js, f"{name} 仍有手写错误提取样板"
        assert "'HTTP ' + resp.status" not in js, f"{name} 仍有手拼 HTTP 状态串"
    admin_js = (STATIC_DIR / "js" / "admin.js").read_text(encoding="utf-8")
    assert "window.ensureOkResponse = ensureOkResponse;" in admin_js
    for name in throwing_subdomains:
        js = (STATIC_DIR / "js" / name).read_text(encoding="utf-8")
        assert "ensureOkResponse(" in js, f"{name} 未走统一错误提取"


def test_tailwind_runtime_loads_before_config():
    """tailwind.min.js（定义全局 tailwind）必须先于 tailwind-config.js（赋值
    tailwind.config）加载；否则抛 `tailwind is not defined`，自定义主题类
    （brand/ink/surface 等）不生成，页面开关等 UI 不渲染。"""
    tailwind_script = '"/admin/static/tailwind.min.js?v='
    config_script = '"/admin/static/js/tailwind-config.js?v='
    for html_file in (
        "index.html",
        "admin-login.html",
        "stream-test.html",
        "request-analyzer.html",
    ):
        html = (STATIC_DIR / html_file).read_text(encoding="utf-8")
        runtime_idx = html.index(tailwind_script)
        config_idx = html.index(config_script)
        assert runtime_idx < config_idx, f"{html_file}: runtime 必须在 config 之前加载"


def test_admin_login_uses_shared_admin_styles_for_primary_button():
    """登录页主按钮必须走共享 admin.css 的 btn-primary（样式与页面内联解耦）。
    属性相邻顺序不再锁定，只断言类与 type 语义存在。"""
    html = LOGIN_HTML.read_text(encoding="utf-8")

    assert 'href="/admin/static/css/admin.css?v=' in html
    submit_btn = next(line for line in html.splitlines() if 'id="submitBtn"' in line)
    assert "btn-primary" in _tag_classes(submit_btn)
    assert 'type="submit"' in submit_btn


def _tag_classes(tag: str) -> set[str]:
    m = re.search(r'class="([^"]*)"', tag)
    return set(m.group(1).split()) if m else set()


def test_admin_index_form_controls_have_accessible_names():
    """无障碍契约：静态外壳的表单控件保持 label[for] 绑定；渠道接入点编辑
    迁入 JS 动态卡片后，卡片模板控件的 aria-label / label 包裹 / policy
    label[for] 绑定契约由 node 行为测试守护（tests/test_channels_frontend.mjs，
    经 renderEndpointCard 真实求值断言），不再对源码做模板字符串解析。"""
    html = INDEX_HTML.read_text(encoding="utf-8")

    # 静态外壳保留的控件：label[for] 绑定不回退
    for control_id in (
        "f_name",
        "f_api_key",
        "f_weight",
        "f_priority",
        "fk_name",
        "testModelSelect",
    ):
        assert f'for="{control_id}"' in html

    assert 'id="tabMobileSelect" aria-label="选择管理页面"' in html

    # 接入点编辑区：容器存在，添加按钮有可见文本（可访问名来自内容）
    assert 'id="endpointsContainer"' in html
    add_endpoint_btn = next(line for line in html.splitlines() if 'id="addEndpointBtn"' in line)
    assert "addEndpointCard()" in add_endpoint_btn
    assert ">+ 添加接入点</button>" in add_endpoint_btn


def test_admin_index_uses_css_classes_instead_of_inline_styles():
    html = INDEX_HTML.read_text(encoding="utf-8")
    css = (STATIC_DIR / "css" / "admin.css").read_text(encoding="utf-8")

    assert " style=" not in html
    assert ".modal-panel" in css
    assert "box-shadow" in css


def test_admin_tool_pages_keep_offline_and_default_port_contracts():
    analyzer_html = (STATIC_DIR / "request-analyzer.html").read_text(encoding="utf-8")
    stream_test_html = (STATIC_DIR / "stream-test.html").read_text(encoding="utf-8")

    assert "cdnjs.cloudflare.com" not in analyzer_html
    assert "/admin/static/vendor/vs2015.min.css?v=" in analyzer_html
    assert "http://localhost:55555/v1/chat/completions" in stream_test_html
    assert "http://localhost:8000/v1/chat/completions" not in stream_test_html


def test_session_viewer_passes_event_explicitly_for_chunk_details():
    html = (STATIC_DIR / "session-viewer.html").read_text(encoding="utf-8")

    assert 'onclick="showChunkDetail(${index}, event)"' in html
    assert "function showChunkDetail(chunkIndex, evt)" in html
    assert "evt.target.closest('.turn')" in html
    assert "event.target.closest('.turn')" not in html


def test_modal_infrastructure_lives_in_modal_js_and_loads_before_consumers():
    """Modal 基础设施（确认框 + 动画收尾 + focus trap）已从 channels.js 搬入
    modal.js，且必须在所有消费方（channels/apikeys/model_groups）之前加载。"""
    modal_js = (STATIC_DIR / "js" / "modal.js").read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")

    # modal.js 提供窄门面 open/close/confirm，接口远小于内部实现（动画/focus trap/loading）
    assert "window.ModalManager = {" in modal_js
    assert "open: openModal," in modal_js
    assert "close: closeModal," in modal_js
    assert "confirm," in modal_js
    # 保留内联 onclick 需要的全局兼容名
    assert "window.closeModal = " in modal_js
    assert "window.showConfirmModal = confirm;" in modal_js
    assert "window.closeConfirmModal = closeConfirm;" in modal_js

    # 加载顺序：modal.js 必须先于渠道各子域模块（消费方在定义时引用门面）
    modal_idx = html.index("/admin/static/js/modal.js?v=")
    for consumer in ("channels_table.js", "channels_modals.js", "channels_editor.js"):
        consumer_idx = html.index(f"/admin/static/js/{consumer}?v=")
        assert modal_idx < consumer_idx, f"modal.js 必须先于 {consumer}"


def test_confirm_modal_contract_moved_out_of_channels_js():
    """channels 模块各子域不再定义确认框实现；统一走 ModalManager 门面
    （渠道主弹窗 CRUD 现居 channels_editor.js，票 04）。"""
    channel_modules = [
        (STATIC_DIR / "js" / "channels_table.js").read_text(encoding="utf-8"),
        (STATIC_DIR / "js" / "channels_modals.js").read_text(encoding="utf-8"),
        (STATIC_DIR / "js" / "channels_editor.js").read_text(encoding="utf-8"),
    ]

    for js in channel_modules:
        assert "function showConfirmModal(" not in js
        assert "function closeConfirmModal(" not in js
        assert "function confirmAction(" not in js
        assert "function closeModal(" not in js
        assert "let pendingConfirmAction = null;" not in js
    # 全部经 ModalManager 通用 open/close/confirm 门面（渠道主弹窗 CRUD 在 editor）
    editor_js = channel_modules[2]
    assert "ModalManager.confirm(" in editor_js
    assert "ModalManager.open(" in editor_js
    assert "ModalManager.close(" in editor_js


def test_apikeys_and_model_groups_use_modal_manager_not_channels_globals():
    """删除测试：apikeys / model_groups 的确认框不再依赖 channels.js 的全局函数，
    而是经 ModalManager 门面；同时保持全局兼容名对 index.html onclick 可见。"""
    apikeys_js = (STATIC_DIR / "js" / "apikeys.js").read_text(encoding="utf-8")
    model_groups_js = (STATIC_DIR / "js" / "model_groups.js").read_text(encoding="utf-8")

    assert "ModalManager.confirm(" in apikeys_js
    assert "ModalManager.confirm(" in model_groups_js
    # 不再定义自己的确认框实现
    assert "function showConfirmModal(" not in apikeys_js
    assert "function showConfirmModal(" not in model_groups_js
    # 兼容层仍存在，index.html 的 onclick="closeConfirmModal()" 可用
    assert 'onclick="closeConfirmModal()"' in INDEX_HTML.read_text(encoding="utf-8")
    assert 'onclick="confirmAction()"' in INDEX_HTML.read_text(encoding="utf-8")


def test_all_modals_go_through_modal_manager_facade():
    """所有业务模态框（含请求详情）统一走 ModalManager.open/close，
    不再由业务模块直接 classList 开合；否则动画收尾与 focus trap 被绕过。"""
    requests_js = (STATIC_DIR / "js" / "requests.js").read_text(encoding="utf-8")

    assert "ModalManager.open(document.getElementById('requestDetailModal'))" in requests_js
    assert "ModalManager.close(document.getElementById('requestDetailModal'))" in requests_js
    assert "requestDetailModal').classList.remove('hidden')" not in requests_js
    assert "requestDetailModal').classList.add('hidden')" not in requests_js


def test_confirm_action_defined_once_in_modal_js_only():
    """confirmAction 双定义冲突已消除：唯一实现位于 modal.js（模态版），
    utils.js 不再声明同名函数覆盖。"""
    utils_js = (STATIC_DIR / "js" / "utils.js").read_text(encoding="utf-8")
    modal_js = (STATIC_DIR / "js" / "modal.js").read_text(encoding="utf-8")

    assert "function confirmAction(" not in utils_js
    assert "window.confirmAction = " not in utils_js
    assert "function confirmAction(" in modal_js
    assert "window.confirmAction = confirmAction;" in modal_js


def test_tab_runtime_loads_before_business_modules_and_is_registered():
    """TabRuntime 深模块：tab_runtime.js 必须在所有业务模块之前加载（业务模块
    加载时即 self-register），index.html 已正确排序。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    tab_js = (STATIC_DIR / "js" / "tab_runtime.js").read_text(encoding="utf-8")

    assert "window.TabRuntime = {" in tab_js
    assert "register," in tab_js
    assert "activate," in tab_js
    assert "deactivate," in tab_js
    assert "bootstrap," in tab_js

    # 加载顺序：tab_runtime.js 必须先于所有业务模块
    tab_idx = html.index("/admin/static/js/tab_runtime.js?v=")
    for biz in (
        "channels_table.js",
        "channels_modals.js",
        "channels_editor.js",
        "apikeys.js",
        "stats.js",
        "requests.js",
        "settings.js",
        "model_groups.js",
        "whitelist.js",
        "storage.js",
        "context_shaping.js",
    ):
        biz_idx = html.index(f"/admin/static/js/{biz}?v=")
        assert tab_idx < biz_idx, f"tab_runtime.js 必须先于 {biz}"


def test_every_tab_registers_once_with_tab_runtime():
    """新增 tab = 模块内一次注册：9 个 tab 各自在所属模块里注册一次，
    外壳不再维护 switch/就绪/清理 的 6 处编辑。"""
    registrations = {
        "channels": ("channels_editor.js", "'channels'"),
        "apikeys": ("apikeys.js", "'apikeys'"),
        "lb": ("model_groups.js", "'lb'"),
        "stats": ("stats.js", "'stats'"),
        "requests": ("requests.js", "'requests'"),
        "settings": ("settings.js", "'settings'"),
        "whitelist": ("whitelist.js", "'whitelist'"),
        "storage": ("storage.js", "'storage'"),
        "context-shaping": ("context_shaping.js", "'context-shaping'"),
    }
    for tab, (module, needle) in registrations.items():
        js = (STATIC_DIR / "js" / module).read_text(encoding="utf-8")
        assert f"window.TabRuntime.register({needle}" in js, f"{module} 未注册 tab {tab}"
        assert js.count(f"window.TabRuntime.register({needle}") == 1


def test_inline_tab_scripts_migrated_to_modules():
    for frag in ("storage.html", "context-shaping.html"):
        html = (STATIC_DIR / "fragments" / "admin" / frag).read_text(encoding="utf-8")
        assert "<script>" not in html
        assert "</script>" not in html

    storage_js = (STATIC_DIR / "js" / "storage.js").read_text(encoding="utf-8")
    assert "window.adminStorage = { load: loadStorageStats };" in storage_js


def test_timers_owned_by_tab_lifecycle_deactivate():
    """定时器归生命周期所有：stats 30s / requests 5s 各自的 deactivate 钩子负责
    停掉后台工作，switchTab 不再裸调 _stopStatsAutoRefresh。"""
    stats_js = (STATIC_DIR / "js" / "stats.js").read_text(encoding="utf-8")
    requests_js = (STATIC_DIR / "js" / "requests.js").read_text(encoding="utf-8")
    admin_js = (STATIC_DIR / "js" / "admin.js").read_text(encoding="utf-8")

    assert "deactivate() {\n        _stopStatsAutoRefresh();\n    }," in stats_js
    assert "deactivate() {\n        _stopRequestsAutoRefresh();\n    }," in requests_js
    # 外壳不再裸调统计定时器
    assert "_stopStatsAutoRefresh" not in admin_js


def test_tab_runtime_keeps_pending_hash_until_restore_applies():
    """深链恢复契约：restore 返回 true（DOM 就绪、已应用）才消费 pendingHash；
    返回 false（片段未加载）必须保留，避免早期 bootstrap 丢深链。"""
    tab_js = (STATIC_DIR / "js" / "tab_runtime.js").read_text(encoding="utf-8")
    requests_js = (STATIC_DIR / "js" / "requests.js").read_text(encoding="utf-8")

    assert "const applied = hooks.restore(_pendingHash) === true;" in tab_js
    assert "if (applied) {" in tab_js
    assert "_pendingHash = '';" in tab_js
    # requests.restore 在 DOM 未就绪时返回 false，应用成功才返回 true
    # （6defa16 将来源筛选变量 sourceBox 改名 sourceEl，指纹随代码同步）
    assert "if (!modelEl || !startEl || !endEl || !successEl || !apiKeyEl || !sourceEl) return false;" in requests_js
    assert "return true;" in requests_js


def test_viewer_escape_html_consolidated_into_escape_js():
    """候选 4：独立工具页手抄的 escapeHtml 合并为 escape.js 唯一实现。
    三个工具页（request-analyzer / json-viewer / session-viewer）加载 escape.js，
    不再各自定义；XSS 修复只改一个地方。"""
    escape_js = (STATIC_DIR / "js" / "escape.js").read_text(encoding="utf-8")
    assert "window.escapeHtml = esc;" in escape_js
    assert "window.esc = esc;" in escape_js

    for page in ("json-viewer.html", "session-viewer.html"):
        html = (STATIC_DIR / page).read_text(encoding="utf-8")
        assert "/admin/static/js/escape.js?v=" in html, f"{page} 未加载 escape.js"
        assert "function escapeHtml(" not in html, f"{page} 仍手抄 escapeHtml"
        # 页面脚本仍使用 escapeHtml（解析到全局），确保合并后引用完好
        assert "escapeHtml(" in html, f"{page} 的 escapeHtml 引用丢失"

    analyzer_html = (STATIC_DIR / "request-analyzer.html").read_text(encoding="utf-8")
    assert "/admin/static/js/escape.js?v=" in analyzer_html
    assert "function escapeHtml(" not in analyzer_html

    analyzer_js = (STATIC_DIR / "js" / "request-analyzer.js").read_text(encoding="utf-8")
    assert "function escapeHtml(" not in analyzer_js
    assert "escapeHtml(" in analyzer_js  # 引用仍在，解析到全局
    assert "function escapeAttr(" in analyzer_js  # 独立 ID 清理器保留，非 HTML 转义

    # 加载顺序：escape.js 必须先于页面自身脚本
    for page, own in (("request-analyzer.html", "request-analyzer.js"),):
        html = (STATIC_DIR / page).read_text(encoding="utf-8")
        assert html.index("/admin/static/js/escape.js?v=") < html.index(f"/admin/static/js/{own}?v=")
