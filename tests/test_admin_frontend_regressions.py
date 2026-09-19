"""管理端前端回归测试（settings schema/绑定面，ADR-0018 产物 + 挂载点不变量）。

ADR-0020 D0：本文件原有的 requests/analyzer/stats 拼写级指纹断言已按删除优先
原则清理——守护真行为的迁移为 node 行为测试（tests/test_tab_lifecycle.mjs、
tests/test_request_analyzer_tool_events.mjs、tests/test_settings_binder.mjs），
纯拼写级（变量名/相邻顺序/出现次数/否定多行）直接删除。settings 挂载点与
绑定器不变量守护真行为，原样保留。
"""

import re
from pathlib import Path

import config

STATIC_JS = Path("static/js")
SETTINGS_FRAGMENT = Path("static/fragments/admin/settings.html")

# ADR-0018 D2：设置页 schema 渲染分区（格式转换与 pii-filter 保持手写交互壳）
SCHEMA_RENDERED_SECTIONS = {"server", "request", "lb", "timezone", "database", "security"}


def _settings_html() -> str:
    return SETTINGS_FRAGMENT.read_text(encoding="utf-8")


def _settings_schema_mounts() -> set[str]:
    return set(re.findall(r'data-schema-group="([^"]+)"', _settings_html()))


def _element_classes(tag_html: str) -> set[str]:
    m = re.search(r'class="([^"]*)"', tag_html)
    return set(m.group(1).split()) if m else set()


def test_settings_page_mounts_exist_for_all_schema_rendered_groups():
    """票03：六个渲染分区的每个描述分组都有挂载点（挂载点 × 描述覆盖不变量）。

    新增设置项只需登记 schema/UI 元数据；若其 section:group 没有对应挂载点，
    字段将无处渲染——本测试立即红。"""
    html = _settings_html()
    for key, meta in config._CONFIG_UI_META.items():
        if meta["section"] in SCHEMA_RENDERED_SECTIONS:
            mount = f'data-schema-group="{meta["section"]}:{meta["group"]}"'
            assert mount in html, f"{key} 所在分组缺少挂载点 {mount}"


def test_settings_page_mounts_all_reference_existing_descriptor_groups():
    """反向不变量：HTML 里的每个挂载点都有描述分组与之对应（无死挂载点）。"""
    covered = {f"{meta['section']}:{meta['group']}" for meta in config._CONFIG_UI_META.values()}
    dead = _settings_schema_mounts() - covered
    assert not dead, f"挂载点没有对应描述分组: {sorted(dead)}"


def test_settings_page_has_no_handwritten_controls_for_mounted_keys():
    """票03：渲染分区键的手写控件必须清零（否则同键双渲染、绑定行为不确定）。"""
    html = _settings_html()
    for key, meta in config._CONFIG_UI_META.items():
        if meta["section"] in SCHEMA_RENDERED_SECTIONS:
            assert f'id="set_{key}"' not in html, f"{key} 仍保留手写控件（双重渲染）"


def test_settings_lb_controls_render_from_schema_mounts():
    """票03：lb 分区控件由 schema 渲染——挂载点存在、手写 select 选项与字段 id 清零，
    条件显隐容器 sticky_lb_options 保留手写（交互壳，初始隐藏）。"""
    html = _settings_html()

    assert 'data-schema-group="lb:0"' in html
    sticky_tag = next(line for line in html.splitlines() if 'id="sticky_lb_options"' in line)
    assert "hidden" in _element_classes(sticky_tag), "sticky_lb_options 必须初始隐藏（syncLbStrategyMode 切换）"
    # 手写控件与选项字面量已由渲染取代（choices 来自描述 choice_label_keys）
    for fragment in (
        'id="set_lb_strategy"',
        'id="set_sticky_ttl"',
        'id="set_sticky_cache_max_entries"',
        'value="round_robin"',
        'value="backup"',
        'value="sticky"',
    ):
        assert fragment not in html, f"lb 手写控件残留: {fragment}"


def test_settings_group_probe_fields_render_from_schema_mount():
    """票03：探活三字段由 lb:3 挂载渲染；min/max 指纹改由描述端点契约测试守护
    （tests/routers/test_settings_schema.py），HTML 不再复制约束字面量。"""
    html = _settings_html()

    assert 'data-schema-group="lb:3"' in html
    for fragment in (
        'id="set_group_probe_interval_seconds"',
        'id="set_group_probe_concurrency"',
        'id="set_group_probe_timeout"',
    ):
        assert fragment not in html, f"探活手写控件残留: {fragment}"


def test_settings_schema_contract_guards_group_probe_bounds():
    """原 HTML min/max 指纹的替代守卫：探活三键的 wire 刻度约束在描述端点登记。"""
    constraints = config._CONFIG_CONSTRAINTS
    assert constraints["group_probe_interval_seconds"]["min"] == 1
    assert constraints["group_probe_interval_seconds"]["max"] == 86400
    assert constraints["group_probe_concurrency"]["min"] == 1
    assert constraints["group_probe_concurrency"]["max"] == 100
    assert constraints["group_probe_timeout"]["min"] == 1
    assert constraints["group_probe_timeout"]["max"] == 300


def test_settings_js_binder_invariants_are_schema_driven():
    """票02：绑定器级不变量——字段唯一来源是描述端点，逐字段手写清单/兜底字面量/
    单位换算特例/派生键/手拼 CSRF 零残留。（ADR-0018 产物，原样保留）"""
    js = Path("static/js/settings.js").read_text(encoding="utf-8")

    # 字段集合唯一来源：描述端点（缓存 promise 单次拉取）
    assert "/admin/settings/schema" in js
    assert "_ensureSchema" in js
    # set_<key> 元素 id 约定保留（存量 KB 后缀 id 以别名登记，不承载行为知识）
    assert "'set_' + key" in js
    assert "elementIdForKey" in js
    # 字节↔显示换算唯一实现（UNIT_SCALE 单点）
    assert js.count("const UNIT_SCALE") == 1
    assert "function wireToDisplay(" in js
    assert "function displayToWire(" in js
    # 数值解析/回退唯一实现：空串与 NaN 统一回退描述默认（NaN 类缺陷的回归钉）
    assert js.count("function parseDisplayValue(") == 1
    assert js.count("function inputToWire(") == 1
    assert "Number.isFinite" in js
    # 脏判定/diff 由描述 section 驱动，无逐字段 section 字符串映射
    assert "function computeDirtySections(" in js
    assert "function buildSavePayload(" in js
    assert "_settingsDirtySections.add(" not in js
    # JS 兜底字面量零残留（历史上 `?? 300` / `|| 10` 类"会撒谎的文档"）
    assert not re.search(r"\?\? *\d", js), "残留 `?? <数值>` 兜底字面量"
    assert not re.search(r"\|\| *\d", js), "残留 `|| <数值>` 兜底字面量"
    # 派生键零残留（GET 收缩为裸 wire 值；渲染 id 一律 set_<key>，KB 后缀别名已随二期删除）
    assert "max_body_size_mb" not in js
    assert ".max_log_body_size_kb" not in js
    assert "orig.max_log_body_size_kb" not in js
    # 手拼 CSRF 零残留（统一走 admin.js 全局 fetch 包装）
    assert "x-csrf-token" not in js.lower()
    # 事件绑定不依赖 .settings-input class 枚举（顺带修复 database 分区 checkbox 脏标记）
    assert "querySelectorAll('.settings-input')" not in js
    # 软约束 warnings 呈现 + 结构化 400 message 提取
    assert "warnings" in js
    assert "function extractDetailMessage(" in js


def test_settings_js_has_no_per_field_enumeration_of_data_keys():
    """票02：纯数据键不再逐字段出现在 JS 里——新增设置项前端零改动。
    （lb_strategy/aggregation_timezone/pii_preset_* 属交互壳，见上与 _runPiiTest。）
    （ADR-0018 产物，原样保留）"""
    js = Path("static/js/settings.js").read_text(encoding="utf-8")

    for fragment in (
        "request_timeout",
        "max_fail_count",
        "cooldown_seconds",
        "rate_limit_wait",
        "sticky_ttl",
        "sticky_cache_max_entries",
        "group_probe_interval_seconds",
        "group_probe_concurrency",
        "group_probe_timeout",
        "max_stream_chunks",
        "retention_days",
        "save_files",
        "admin_max_attempts",
        "admin_lockout_base_seconds",
    ):
        assert fragment not in js, f"逐字段枚举残留: {fragment}"
