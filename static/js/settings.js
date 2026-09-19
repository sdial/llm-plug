/**
 * settings.js — 设置页 schema 驱动通用绑定器 + 控件渲染（ADR-0018 D1/D2/D3）。
 *
 * 加载/脏检测/保存/校验全部由 GET /admin/settings/schema 的字段描述驱动：
 * 字段清单、默认值、单位换算不再手写，新增设置项前端零改动。描述的 default
 * 是唯一兜底来源（JS 侧数值/字面量兜底零残留）；wire 是唯一权威刻度，
 * 字节↔显示换算由 UNIT_SCALE 单点完成。
 *
 * 纯函数区（解析归一/单位换算/脏判定/diff 载荷/校验/detail 提取 + D2 的 HTML
 * 渲染构造）不碰 DOM，由 tests/test_settings_binder.mjs 与
 * tests/test_settings_render.mjs 经 IIFE 提取法直测。渲染：片段里的
 * data-schema-group="<section>:<group>" 挂载点由描述按 section/group/顺序填充
 * （server/request/lb/timezone/database/security 六分区）；交互壳（lb_strategy
 * 联动、时区下拉填充、PII 编辑器/确认弹窗、改密表单、格式转换面板）不进
 * 通用循环，其数据键仍走描述。
 */
(() => {

// ══════════════ 纯函数区（无 DOM/fetch 依赖，node --test 直测） ══════════════

// 显示单位 → wire 字节的唯一换算表。只有描述带 unit 的键参与换算，其余原样透传。
const UNIT_SCALE = { MB: 1024 * 1024, KB: 1024 };

function wireToDisplay(wire, descriptor) {
  const scale = descriptor && UNIT_SCALE[descriptor.unit];
  if (!scale) return wire;
  return Math.floor(wire / scale);
}

function displayToWire(display, descriptor) {
  const scale = descriptor && UNIT_SCALE[descriptor.unit];
  if (!scale) return display;
  return Math.round(display * scale);
}

// 输入框原始文本 → 类型化显示值。空串/NaN 统一回退描述 default（显示刻度），
// 是对历史上 `|| 默认` 与 `?? 默认` 两种语义的统一——清空输入不再产生永久脏
// 标记，也不再把 NaN 序列化成 null 送服务端换来 400。
function parseDisplayValue(raw, descriptor) {
  const num = descriptor.type === 'float' ? parseFloat(raw) : parseInt(raw, 10);
  if (!Number.isFinite(num)) return wireToDisplay(descriptor.default, descriptor);
  return num;
}

function inputToWire(raw, descriptor) {
  return displayToWire(parseDisplayValue(raw, descriptor), descriptor);
}

// 描述的 min/max（wire 刻度，与服务端校验同源同值）→ 显示刻度边界：
// min 向上取整、max 向下取整，保证前端放行的显示值换算回 wire 后必被服务端接受。
function displayBounds(descriptor) {
  const scale = descriptor && UNIT_SCALE[descriptor.unit];
  const bounds = {};
  if (descriptor.min !== undefined) {
    bounds.min = scale ? Math.ceil(descriptor.min / scale) : descriptor.min;
  }
  if (descriptor.max !== undefined) {
    bounds.max = scale ? Math.floor(descriptor.max / scale) : descriptor.max;
  }
  return bounds;
}

// wire 刻度校验：choices 成员资格 + min/max 区间。返回 null 或
// { type: 'range'|'choices', min?, max? }（range 附显示刻度边界供文案使用）。
function validateWireValue(wire, descriptor) {
  if (descriptor.choices && !descriptor.choices.includes(wire)) {
    return { type: 'choices' };
  }
  const hasMin = descriptor.min !== undefined;
  const hasMax = descriptor.max !== undefined;
  if ((!hasMin || wire >= descriptor.min) && (!hasMax || wire <= descriptor.max)) {
    return null;
  }
  const bounds = displayBounds(descriptor);
  return { type: 'range', min: bounds.min, max: bounds.max };
}

// 脏判定：current 里的每个非只读键与原值比较，分区归属取自描述的 section。
function computeDirtySections(descriptors, original, current) {
  const dirty = new Set();
  Object.keys(current).forEach((key) => {
    const descriptor = descriptors[key];
    if (!descriptor || descriptor.readonly) return;
    if (current[key] !== original[key]) dirty.add(descriptor.section);
  });
  return dirty;
}

// diff 载荷：仅上送 wire 值发生变更的非只读键。
function buildSavePayload(descriptors, original, current) {
  const payload = {};
  Object.keys(current).forEach((key) => {
    const descriptor = descriptors[key];
    if (!descriptor || descriptor.readonly) return;
    if (current[key] !== original[key]) payload[key] = current[key];
  });
  return payload;
}

// 结构化 400 detail → message 文本（修复 "[object Object]" 展示）。
function extractDetailMessage(detail) {
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') {
    return detail.message;
  }
  return null;
}

// ══════════════ 纯渲染函数区（描述 → HTML 字符串，D2 二期） ══════════════
//
// 手写 HTML 的可见内容等价基线：label/help 走文案键（data-i18n + 当前语言文案）、
// 热更新 pill 按 hot 显式元数据、input 的 min/max 由 wire 刻度经 displayBounds
// 换算到显示刻度、choices 选项与文案键一一对应、只读样式与后缀 flex 行保持现状。
// 控件 id 一律 set_<key>，事件绑定/脏检测/保存复用既有按 id 约定的绑定循环。

// 挂载点属性 data-schema-group="<section>:<group>" → { section, group }。
function parseSchemaGroupSpec(attr) {
  const sep = attr.indexOf(':');
  if (sep <= 0) return null;
  const section = attr.slice(0, sep);
  const group = Number(attr.slice(sep + 1));
  if (!section || !Number.isInteger(group)) return null;
  return { section, group };
}

// 挂载点匹配：取 section+group 相符的描述（含键名），保持 schema 迭代顺序
// （config._CONFIG_UI_META 键序 = 分区内视觉顺序）。
function descriptorsForMount(descriptors, section, group) {
  const fields = [];
  Object.keys(descriptors).forEach((key) => {
    const descriptor = descriptors[key];
    if (descriptor.section === section && descriptor.group === group) {
      fields.push({ key, descriptor });
    }
  });
  return fields;
}

function elementIdForKey(key) {
  return 'set_' + key;
}

function renderInputClass(descriptor) {
  if (descriptor.readonly) {
    return 'w-full px-3 py-2.5 text-sm bg-surface-50 ' + (descriptor.input_class || 'text-ink-400');
  }
  return 'w-full px-3 py-2.5 text-sm settings-input' + (descriptor.input_class ? ' ' + descriptor.input_class : '');
}

// label 文案（含随 label 渲染的热更新 pill；组级标题 pill 保留手写，不在此渲染）。
function renderLabelHtml(descriptor, elementId, t) {
  const labelClass = descriptor.label_class || 'block text-sm font-medium text-ink-800 mb-1.5';
  let inner = '<span data-i18n="' + descriptor.label_key + '">' + esc(t(descriptor.label_key)) + '</span>';
  if (descriptor.hot) {
    inner += ' <span class="pill pill-success ml-2" data-i18n="settings.hotReload">' + esc(t('settings.hotReload')) + '</span>';
  }
  return '<label for="' + elementId + '" class="' + labelClass + '">' + inner + '</label>';
}

// help 行：静态文案键（help_key/help_class）或策略型下拉的动态说明占位行——
// 后者文案由交互壳事件绑定（syncLbStrategyMode 按 choice_help_keys）填充。
function renderHelpHtml(descriptor, elementId, t) {
  if (descriptor.help_key) {
    const helpClass = descriptor.help_class || 'text-xs text-ink-500 mt-1';
    return '<p class="' + helpClass + '" data-i18n="' + descriptor.help_key + '">' + esc(t(descriptor.help_key)) + '</p>';
  }
  if (descriptor.choices && descriptor.choice_help_keys) {
    return '<p id="' + elementId + '_help" class="text-xs text-ink-500 mt-1"></p>';
  }
  return '';
}

function renderChoiceOptions(descriptor, t) {
  const labelKeys = descriptor.choice_label_keys || {};
  return descriptor.choices.map((value) => {
    const labelKey = labelKeys[value];
    const label = labelKey ? esc(t(labelKey)) : esc(value);
    const i18nAttr = labelKey ? ' data-i18n="' + labelKey + '"' : '';
    return '<option value="' + esc(value) + '"' + i18nAttr + '>' + label + '</option>';
  }).join('');
}

function renderBlankOption(descriptor, t) {
  const key = descriptor.blank_option_key;
  return '<option value="" data-i18n="' + key + '">' + esc(t(key)) + '</option>';
}

function renderSelectHtml(elementId, inputClass, optionsHtml) {
  return '<select id="' + elementId + '" class="' + inputClass + '">' + optionsHtml + '</select>';
}

// int/float → number、其余 → text；min/max 由 wire 刻度换算到显示刻度
// （displayBounds：min 向上取整、max 向下取整），readonly 语义与手写版一致。
function renderInputHtml(descriptor, elementId) {
  const isNumeric = descriptor.type === 'int' || descriptor.type === 'float';
  const bounds = displayBounds(descriptor);
  let attrs = ' type="' + (isNumeric ? 'number' : 'text') + '"';
  attrs += ' id="' + elementId + '"';
  if (bounds.min !== undefined) attrs += ' min="' + bounds.min + '"';
  if (bounds.max !== undefined) attrs += ' max="' + bounds.max + '"';
  if (descriptor.readonly) attrs += ' readonly';
  return '<input' + attrs + ' class="' + renderInputClass(descriptor) + '">';
}

function renderControlHtml(descriptor, elementId, t) {
  if (descriptor.choices) {
    return renderSelectHtml(elementId, renderInputClass(descriptor), renderChoiceOptions(descriptor, t));
  }
  if (descriptor.blank_option_key) {
    return renderSelectHtml(elementId, renderInputClass(descriptor), renderBlankOption(descriptor, t));
  }
  return renderInputHtml(descriptor, elementId);
}

// bool → checkbox：label 包裹控件、文案键在文本 span 上（database 分区手写版结构）。
function renderCheckboxControl(descriptor, key, t) {
  return '<label class="flex items-center gap-2 text-sm text-ink-700 cursor-pointer">'
    + '<input type="checkbox" id="' + elementIdForKey(key) + '"> '
    + '<span data-i18n="' + descriptor.label_key + '">' + esc(t(descriptor.label_key)) + '</span></label>';
}

// 后缀 flex 行（如保留天数 × "天"）：input 与后缀文案同处一行。
function renderSuffixWrapper(descriptor, elementId, t, inputHtml) {
  return '<div class="flex items-center gap-2">' + inputHtml
    + '<span class="text-sm text-ink-500 flex-shrink-0" data-i18n="' + descriptor.suffix_key + '">'
    + esc(t(descriptor.suffix_key)) + '</span></div>';
}

function renderControlWithSuffix(descriptor, elementId, t) {
  return renderSuffixWrapper(descriptor, elementId, t, renderInputHtml(descriptor, elementId));
}

// 单个字段渲染（bool → 裸 checkbox label；其余 → div 包裹 label+控件+help）。
function renderFieldControl(descriptor, key, t) {
  if (descriptor.type === 'bool') return renderCheckboxControl(descriptor, key, t);
  const elementId = elementIdForKey(key);
  const control = descriptor.suffix_key
    ? renderControlWithSuffix(descriptor, elementId, t)
    : renderControlHtml(descriptor, elementId, t);
  return '<div>' + renderLabelHtml(descriptor, elementId, t) + control + renderHelpHtml(descriptor, elementId, t) + '</div>';
}

// 组渲染：挂载点内的字段控件序列（顺序 = 传入序 = schema 迭代序）。
function renderGroupControls(fields, t) {
  return fields.map((field) => renderFieldControl(field.descriptor, field.key, t)).join('');
}

// ══════════════ DOM 粘合层 ══════════════

let _settingsOriginal = {};
let _settingsCurrentSection = 'server';
let _settingsDirtySections = new Set();
let _settingsInitRoot = null;
let _schema = null;
let _schemaPromise = null;

// 字段描述获取：缓存 promise，整个会话只拉一次（描述是 config 的只读投影）。
function _ensureSchema() {
  if (_schema) return Promise.resolve(_schema);
  if (!_schemaPromise) {
    _schemaPromise = fetch('/admin/settings/schema')
      .then((resp) => {
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.json();
      })
      .then((schema) => {
        _schema = schema;
        return schema;
      })
      .catch((e) => {
        _schemaPromise = null;
        throw e;
      });
  }
  return _schemaPromise;
}

// 描述键集合驱动的绑定面：元素存在即绑定，不依赖 .settings-input class 枚举。
// （顺带修复：database 分区 checkbox 与 PII 动作下拉缺该 class，此前从不触发
// 脏标记；现按 set_<key> 约定绑定后正常。）
function _bindableKeys() {
  if (!_schema) return [];
  return Object.keys(_schema).filter((key) => document.getElementById(elementIdForKey(key)));
}

function _readWireFromDom(key, descriptor) {
  const el = document.getElementById(elementIdForKey(key));
  if (!el) return undefined;
  if (descriptor.type === 'bool') return Boolean(el.checked);
  if (descriptor.type === 'int' || descriptor.type === 'float') return inputToWire(el.value, descriptor);
  return descriptor.trim ? el.value.trim() : el.value;
}

function _writeValueToDom(key, descriptor, value) {
  const el = document.getElementById(elementIdForKey(key));
  if (!el) return;
  if (descriptor.type === 'bool') {
    el.checked = Boolean(value);
  } else if (descriptor.type === 'int' || descriptor.type === 'float') {
    el.value = wireToDisplay(value, descriptor);
  } else {
    el.value = value;
  }
}

function switchSettingsSection(section) {
  _settingsCurrentSection = section;
  document.querySelectorAll('.settings-section').forEach(el => el.classList.add('hidden'));
  document.getElementById('settings_' + section)?.classList.remove('hidden');
  document.querySelectorAll('.settings-nav-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.section === section);
  });
  if (section === 'format_conversion') {
    loadFormatConversionPanel();
  }
}

function _updateSettingsDirtyIndicators() {
  document.querySelectorAll('.settings-nav-btn').forEach(btn => {
    const dot = btn.querySelector('.settings-dirty-dot');
    if (dot) {
      if (_settingsDirtySections.has(btn.dataset.section)) {
        dot.classList.remove('hidden');
        dot.classList.add('inline-block');
      } else {
        dot.classList.add('hidden');
        dot.classList.remove('inline-block');
      }
    }
  });
}

function _detectSettingsDirty() {
  if (!_schema || !_settingsOriginal || Object.keys(_settingsOriginal).length === 0) return;
  const current = {};
  _bindableKeys().forEach((key) => {
    if (!_schema[key].readonly) current[key] = _readWireFromDom(key, _schema[key]);
  });
  _settingsDirtySections = computeDirtySections(_schema, _settingsOriginal, current);
  _updateSettingsDirtyIndicators();
}

function syncLbStrategyMode() {
  const strategyEl = document.getElementById(elementIdForKey('lb_strategy'));
  const stickyOptions = document.getElementById('sticky_lb_options');
  // 动态说明行由渲染器为策略型下拉生成（id = set_<key>_help），文案按选中项填充
  const help = document.getElementById(elementIdForKey('lb_strategy') + '_help');
  if (!strategyEl || !stickyOptions || !help) return;
  const strategy = strategyEl.value;
  stickyOptions.classList.toggle('hidden', strategy !== 'sticky');
  // 说明文案键来自描述的 choice_help_keys（schema 是文案映射的唯一来源）
  const descriptor = _schema && _schema.lb_strategy;
  const choiceHelpKeys = (descriptor && descriptor.choice_help_keys) || {};
  const helpKey = choiceHelpKeys[strategy];
  if (helpKey) help.textContent = I18n.t(helpKey);
}


// 修改密码表单提交（统一走 admin.js 全局 fetch 包装，自动注入 CSRF）
function _bindChangePasswordForm() {
    const form = document.getElementById('changePasswordForm');
    if (!form || form.dataset.bound === '1') return;
    form.dataset.bound = '1';
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const msg = document.getElementById('cp_message');
        msg.classList.add('hidden');
        msg.classList.remove('text-rose-600', 'text-green-600');

        const old_password = document.getElementById('cp_old_password').value;
        const new_password = document.getElementById('cp_new_password').value;
        const confirm_password = document.getElementById('cp_confirm_password').value;

        if (new_password !== confirm_password) {
            msg.textContent = I18n.t('settings.secPwdMismatch');
            msg.classList.add('text-rose-600');
            msg.classList.remove('hidden');
            return;
        }

        try {
            const resp = await fetch('/admin/auth/change-password', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ old_password, new_password, confirm_password }),
            });

            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                throw new Error(extractDetailMessage(data.detail) || I18n.t('settings.secPwdFailed'));
            }

            msg.textContent = I18n.t('settings.secPwdSuccess');
            msg.classList.add('text-green-600');
            msg.classList.remove('hidden');
            document.getElementById('changePasswordForm').reset();

            // 2秒后跳转到登录页
            setTimeout(() => {
                window.location.href = '/admin/login';
            }, 2000);
        } catch (err) {
            msg.textContent = err.message;
            msg.classList.add('text-rose-600');
            msg.classList.remove('hidden');
        }
    });
}

// 渲染全部挂载点：data-schema-group="<section>:<group>" 由描述填充，随后对挂载
// 子树跑 i18n 根翻译（片段 settle 时的整页翻译发生在渲染之前，这里需补一次；
// 之后语言切换走 I18n.setLang → translatePage 全局重译，data-i18n 属性已就位）。
function _renderSchemaMounts(schema) {
  document.querySelectorAll('[data-schema-group]').forEach((mount) => {
    const spec = parseSchemaGroupSpec(mount.dataset.schemaGroup);
    if (!spec) return;
    const fields = descriptorsForMount(schema, spec.section, spec.group);
    if (fields.length === 0) return;
    mount.innerHTML = renderGroupControls(fields, I18n.t);
    if (window.I18n) I18n.translateRoot(mount);
  });
}

// lb_strategy 策略联动壳（sticky 选项显隐 + 选中项说明行）：内联 onchange 已随
// 手写控件删除，改在 init 阶段事件绑定，交互不变。
function _bindLbStrategyShell() {
  const strategyEl = document.getElementById(elementIdForKey('lb_strategy'));
  if (!strategyEl || strategyEl.dataset.strategySyncBound === '1') return;
  strategyEl.dataset.strategySyncBound = '1';
  strategyEl.addEventListener('change', syncLbStrategyMode);
}

function initSettings() {
  const root = document.getElementById('settings_server') || document.getElementById('settings') || document.getElementById('settings_host');
  if (!root || root === _settingsInitRoot) return;
  _settingsInitRoot = root;
  _renderPiiCustomRules();
  _bindPiiFilterUI();
  // schema 就绪后：渲染挂载点 → 事件绑定（元素存在即绑定，渲染产物与手写交互壳
  // 共用同一套 set_<key> 绑定循环）。
  _ensureSchema()
    .then((schema) => {
      if (_settingsInitRoot !== root) return; // 片段已重建，由新的 initSettings 接管
      _renderSchemaMounts(schema);
      Object.keys(schema).forEach((key) => {
        const el = document.getElementById(elementIdForKey(key));
        if (!el || el.dataset.settingsBound === '1') return;
        el.dataset.settingsBound = '1';
        el.addEventListener('input', () => _detectSettingsDirty());
        el.addEventListener('change', () => _detectSettingsDirty());
      });
      _bindLbStrategyShell();
    })
    .catch((e) => console.error('Failed to load settings schema:', e));
}

// 用 Intl.supportedValuesOf('timeZone') 填充时区下拉，按地区前缀分组。
// currentValue：存量值，可能不在列表中（非法/过旧时区），需补入避免回显丢失。
function _populateTimezoneSelect(currentValue) {
  const sel = document.getElementById('set_aggregation_timezone');
  if (!sel) return;
  // 移除上次动态生成的 option 与 optgroup（保留首个空选项）
  const first = sel.querySelector('option[value=""]');
  Array.from(sel.children).forEach((c) => { if (c !== first) c.remove(); });

  let zones;
  try {
    zones = Intl.supportedValuesOf('timeZone');
  } catch (e) {
    // 极老浏览器不支持：降级为只保留空选项 + 常用区
    zones = ['UTC', 'Asia/Shanghai', 'America/Los_Angeles', 'Europe/London'];
  }
  const present = new Set(zones);
  if (currentValue && !present.has(currentValue)) {
    zones = zones.concat(currentValue); // 存量值若不在列表则补入
    present.add(currentValue);
  }
  // 按 "/" 前缀分组
  const groups = new Map();
  for (const z of zones) {
    const slash = z.indexOf('/');
    const group = slash === -1 ? 'Other' : z.slice(0, slash);
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(z);
  }
  const frag = document.createDocumentFragment();
  const sortedGroups = Array.from(groups.keys()).sort();
  for (const g of sortedGroups) {
    const optgroup = document.createElement('optgroup');
    optgroup.label = g;
    groups.get(g).sort().forEach((z) => {
      const opt = document.createElement('option');
      opt.value = z;
      opt.textContent = z;
      optgroup.appendChild(opt);
    });
    frag.appendChild(optgroup);
  }
  sel.appendChild(frag);
  if (currentValue) sel.value = currentValue;
}

async function loadSettings() {
  try {
    const schema = await _ensureSchema();
    // initSettings 与本函数共用同一次 schema promise：渲染回调先注册，await 返回
    // 时挂载点已填充，set_<key> 元素必然就位（控件不再来自静态手写 HTML）。
    if (!document.getElementById('set_host')) return;
    const resp = await fetch('/admin/settings');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    _settingsOriginal = data;
    _settingsDirtySections = new Set();
    _updateSettingsDirtyIndicators();
    // 通用回填：描述键集合驱动（含安全分区两键，主 GET 单一加载路径）
    Object.keys(schema).forEach((key) => _writeValueToDom(key, schema[key], data[key]));
    _populateTimezoneSelect(data.aggregation_timezone);
    _renderPiiCustomRules();
    syncLbStrategyMode();
    _bindChangePasswordForm();
  } catch (e) {
    console.error('Failed to load settings:', e);
  }
}

function _formatValidationFailure(key, failure) {
  const label = I18n.t(_schema[key].label_key);
  if (failure.type === 'choices') return I18n.t('settings.invalidChoice', { name: label });
  if (failure.max === undefined) return I18n.t('settings.invalidMin', { name: label, min: failure.min });
  if (failure.min === undefined) return I18n.t('settings.invalidMax', { name: label, max: failure.max });
  return I18n.t('settings.invalidRange', { name: label, min: failure.min, max: failure.max });
}

async function saveSettings() {
  if (!_schema) {
    try {
      await _ensureSchema();
    } catch (e) {
      console.error('Failed to load settings schema:', e);
      return;
    }
  }
  // 读取全部可绑定非只读键的 wire 值，并按描述做保存前客户端校验
  const current = {};
  const failures = [];
  _bindableKeys().forEach((key) => {
    const descriptor = _schema[key];
    if (descriptor.readonly) return;
    const wire = _readWireFromDom(key, descriptor);
    current[key] = wire;
    const failure = validateWireValue(wire, descriptor);
    if (failure) failures.push({ key, failure });
  });
  failures.forEach(({ key, failure }) => showGlobalToast(_formatValidationFailure(key, failure), 'error'));
  if (failures.length > 0) return;

  const payload = buildSavePayload(_schema, _settingsOriginal, current);
  if (Object.keys(payload).length === 0) {
    showGlobalToast(I18n.t('settings.noChanges'), 'info');
    return;
  }

  try {
    const resp = await fetch('/admin/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (resp.ok) {
      const result = await resp.json().catch(() => ({}));
      // 软约束告警（如探活间隔 > 冷却期）以非阻断 info 提示呈现
      if (Array.isArray(result.warnings)) {
        result.warnings.forEach((w) => showGlobalToast(w, 'info'));
      }
      showGlobalToast(I18n.t('settings.saveSuccess'), 'success');
      loadSettings();
    } else {
      const err = await resp.json().catch(() => ({}));
      const detail = extractDetailMessage(err.detail) || 'HTTP ' + resp.status;
      showGlobalToast(I18n.t('settings.saveFailed') + ': ' + detail, 'error');
    }
  } catch (e) {
    showGlobalToast(I18n.t('settings.saveFailed') + ': ' + e.message, 'error');
  }
}

function getOriginalSettings() {
    return _settingsOriginal;
}

// ===== 格式转换面板 =====

let _fcChannels = [];
let _fcGlobalAllowed = true;
let _fcLoading = false;

function _fcEffectiveAllowed(ch, globalAllowed) {
  const v = ch.allow_format_conversion;
  if (v === null || v === undefined) return globalAllowed;
  return Boolean(v);
}

function _fcOverrideMeta(ch) {
  const v = ch.allow_format_conversion;
  if (v === null || v === undefined) return { value: '', label: I18n.t('settings.fcFollowGlobal'), cls: 'bg-surface-100 text-ink-500' };
  if (v === true) return { value: 'true', label: I18n.t('settings.fcForceAllow'), cls: 'bg-emerald-50 text-emerald-700' };
  return { value: 'false', label: I18n.t('settings.fcForceBlock'), cls: 'bg-rose-50 text-rose-700' };
}

function _fcRenderChannelRow(ch) {
  const _primaryApiType = (ch.endpoints && ch.endpoints.length ? ((ch.endpoints.find(ep => ep.enabled !== false) || ch.endpoints[0]).api_type) : ch.api_type);
  const apiInfo = API_TYPE_MAP[_primaryApiType] || { short: (_primaryApiType || '?').charAt(0).toUpperCase(), color: 'bg-gray-100 text-gray-700', title: _primaryApiType };
  const override = _fcOverrideMeta(ch);
  const disabled = ch.enabled === false;
  return `
    <div class="flex items-center gap-3 px-4 py-2.5 border-t border-surface-100 first:border-t-0 ${disabled ? 'opacity-60' : ''}">
      <span class="inline-flex items-center justify-center w-6 h-6 rounded-md text-xs font-bold ${apiInfo.color}" title="${esc(apiInfo.title)}">${apiInfo.short}</span>
      <div class="min-w-0 flex-1 flex items-center gap-2">
        <span class="text-sm font-medium text-ink-900 truncate" title="${esc(ch.name)}">${esc(ch.name)}</span>
        ${disabled ? `<span class="text-[0.625rem] px-1.5 py-0.5 rounded bg-surface-200 text-ink-500 flex-shrink-0">${I18n.t('settings.fcDisabledTag')}</span>` : ''}
        <span class="text-[0.625rem] px-1.5 py-0.5 rounded font-medium flex-shrink-0 ${override.cls}">${override.label}</span>
      </div>
      <select data-fc-channel-id="${esc(ch.id)}" class="fc-channel-select text-xs border border-surface-200 rounded-md px-2 py-1.5 bg-white outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500">
        <option value=""${override.value === '' ? ' selected' : ''}>${I18n.t('settings.fcFollowGlobal')}</option>
        <option value="true"${override.value === 'true' ? ' selected' : ''}>${I18n.t('settings.fcForceAllow')}</option>
        <option value="false"${override.value === 'false' ? ' selected' : ''}>${I18n.t('settings.fcForceBlock')}</option>
      </select>
    </div>
  `;
}

function _fcRenderPanel() {
  const panel = document.getElementById('fc_panel');
  const toggle = document.getElementById('fc_global_toggle');
  const status = document.getElementById('fc_global_status');
  if (!panel || !toggle || !status) return;

  toggle.checked = _fcGlobalAllowed;
  status.textContent = _fcGlobalAllowed
    ? I18n.t('settings.fcStatusOn')
    : I18n.t('settings.fcStatusOff');

  const allowed = [];
  const blocked = [];
  for (const ch of _fcChannels) {
    if (_fcEffectiveAllowed(ch, _fcGlobalAllowed)) allowed.push(ch);
    else blocked.push(ch);
  }
  const sortFn = (a, b) => (a.priority - b.priority) || a.name.localeCompare(b.name, I18n.getLocale());
  allowed.sort(sortFn);
  blocked.sort(sortFn);

  const emptyRow = `<div class="px-4 py-6 text-sm text-ink-400 text-center">${I18n.t('settings.fcNoChannels')}</div>`;
  const countUnit = I18n.t('settings.fcCountUnit');
  panel.innerHTML = `
    <div class="card overflow-hidden">
      <div class="flex items-center gap-2 px-4 py-3 bg-emerald-50/60 border-b border-emerald-100">
        <svg class="w-4 h-4 text-emerald-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
        <span class="text-sm font-semibold text-emerald-800">${I18n.t('settings.fcAllowedGroup')}</span>
        <span class="text-xs text-emerald-700">${allowed.length} ${countUnit}</span>
        <span class="text-xs text-ink-500 ml-auto">${I18n.t('settings.fcAllowedHint')}</span>
      </div>
      ${allowed.length ? allowed.map(_fcRenderChannelRow).join('') : emptyRow}
    </div>
    <div class="card overflow-hidden">
      <div class="flex items-center gap-2 px-4 py-3 bg-rose-50/60 border-b border-rose-100">
        <svg class="w-4 h-4 text-rose-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>
        <span class="text-sm font-semibold text-rose-800">${I18n.t('settings.fcBlockedGroup')}</span>
        <span class="text-xs text-rose-700">${blocked.length} ${countUnit}</span>
        <span class="text-xs text-ink-500 ml-auto">${I18n.t('settings.fcBlockedHint')}</span>
      </div>
      ${blocked.length ? blocked.map(_fcRenderChannelRow).join('') : emptyRow}
    </div>
  `;

  panel.querySelectorAll('.fc-channel-select').forEach(sel => {
    sel.addEventListener('change', () => _fcOnChannelChange(sel));
  });
}

async function loadFormatConversionPanel() {
  if (_fcLoading) return;
  _fcLoading = true;
  const panel = document.getElementById('fc_panel');
  if (!panel) { _fcLoading = false; return; }
  try {
    const [settingsResp, channelsResp] = await Promise.all([
      fetch('/admin/settings'),
      fetch('/admin/channels'),
    ]);
    if (!settingsResp.ok) throw new Error(`HTTP ${settingsResp.status}`);
    if (!channelsResp.ok) throw new Error(`HTTP ${channelsResp.status}`);
    const settings = await settingsResp.json();
    const channels = await channelsResp.json();
    _fcGlobalAllowed = settings.allow_format_conversion;
    _fcChannels = Array.isArray(channels) ? channels : [];
    _fcRenderPanel();
    _fcBindGlobalToggle();
  } catch (e) {
    panel.innerHTML = `<div class="text-sm text-rose-600 py-10 text-center">${I18n.t('settings.fcSaveFailedToast')}: ${esc(e.message)}</div>`;
  } finally {
    _fcLoading = false;
  }
}

// 注意：htmx 每次切到设置 Tab 都会重建整个片段（含 fc_global_toggle 元素），
// 所以这里不能依赖持久标志跳过绑定，必须在面板每次渲染后对新元素重新绑定。
// 旧元素已被 _fcRenderPanel 替换丢弃，不会造成重复监听。
function _fcBindGlobalToggle() {
  const toggle = document.getElementById('fc_global_toggle');
  if (!toggle) return;
  toggle.addEventListener('change', _fcOnGlobalToggle);
}

async function _fcOnGlobalToggle(e) {
  const toggle = e.target;
  const desired = toggle.checked;
  const prev = _fcGlobalAllowed;
  toggle.disabled = true;
  try {
    const resp = await fetch('/admin/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ allow_format_conversion: desired }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(extractDetailMessage(err.detail) || ('HTTP ' + resp.status));
    }
    _fcGlobalAllowed = desired;
    if (_settingsOriginal && typeof _settingsOriginal === 'object') {
      _settingsOriginal.allow_format_conversion = desired;
    }
    _fcRenderPanel();
    showGlobalToast(I18n.t('settings.fcSavedToast'), 'success');
  } catch (err) {
    toggle.checked = prev;
    _fcGlobalAllowed = prev;
    showGlobalToast(I18n.t('settings.fcSaveFailedToast') + '：' + err.message, 'error');
  } finally {
    toggle.disabled = false;
  }
}

async function _fcOnChannelChange(sel) {
  const channelId = sel.dataset.fcChannelId;
  const raw = sel.value;
  const payloadValue = raw === '' ? null : raw === 'true';
  const ch = _fcChannels.find(c => c.id === channelId);
  if (!ch) return;
  const prev = ch.allow_format_conversion;
  sel.disabled = true;
  try {
    const resp = await fetch('/admin/channels/' + encodeURIComponent(channelId), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ allow_format_conversion: payloadValue }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(extractDetailMessage(err.detail) || ('HTTP ' + resp.status));
    }
    const updated = await resp.json();
    ch.allow_format_conversion = updated.allow_format_conversion;
    _fcRenderPanel();
    const label = payloadValue === null ? I18n.t('settings.fcFollowGlobal') : (payloadValue ? I18n.t('settings.fcForceAllow') : I18n.t('settings.fcForceBlock'));
    showGlobalToast(`${ch.name}：${label}`, 'success');
  } catch (err) {
    ch.allow_format_conversion = prev;
    _fcRenderPanel();
    showGlobalToast(I18n.t('settings.fcSaveFailedToast') + '：' + err.message, 'error');
  }
}

// ===== PII Filter 自定义规则与实时测试 =====

function _renderPiiCustomRules() {
  const container = document.getElementById('pii_custom_rules_container');
  if (!container) return;
  const raw = document.getElementById('set_pii_custom_rules').value;
  let rules = [];
  try { rules = JSON.parse(raw); } catch (e) {}
  if (!Array.isArray(rules)) rules = [];
  container.innerHTML = rules.map((rule, idx) => `
    <div class="pii-rule-row flex flex-wrap items-center gap-2 p-2 border border-surface-200 rounded-md" data-idx="${idx}">
      <input type="text" placeholder="规则名" class="pii-rule-name text-xs px-2 py-1 border rounded w-24" value="${esc(rule.name || '')}">
      <input type="text" placeholder="entity" class="pii-rule-entity text-xs px-2 py-1 border rounded w-24" value="${esc(rule.entity || '')}">
      <input type="text" placeholder="正则" class="pii-rule-pattern text-xs px-2 py-1 border rounded flex-1 min-w-[7.5rem]" value="${esc(rule.pattern || '')}">
      <select class="pii-rule-action text-xs px-2 py-1 border rounded">
        <option value="mask"${(rule.action || 'mask') === 'mask' ? ' selected' : ''}>mask</option>
        <option value="replace"${rule.action === 'replace' ? ' selected' : ''}>replace</option>
        <option value="block"${rule.action === 'block' ? ' selected' : ''}>block</option>
      </select>
      <button type="button" class="pii-rule-delete text-xs text-rose-600 px-2">×</button>
    </div>
  `).join('');
  container.querySelectorAll('.pii-rule-delete').forEach(btn => {
    btn.addEventListener('click', () => _removePiiRule(parseInt(btn.closest('.pii-rule-row').dataset.idx)));
  });
  container.querySelectorAll('input, select').forEach(el => {
    el.addEventListener('change', _syncPiiCustomRulesFromUI);
    el.addEventListener('input', _detectSettingsDirty);
  });
}

function _syncPiiCustomRulesFromUI() {
  const rows = document.querySelectorAll('.pii-rule-row');
  const rules = [];
  rows.forEach(row => {
    rules.push({
      name: row.querySelector('.pii-rule-name').value,
      entity: row.querySelector('.pii-rule-entity').value,
      pattern: row.querySelector('.pii-rule-pattern').value,
      action: row.querySelector('.pii-rule-action').value,
    });
  });
  document.getElementById('set_pii_custom_rules').value = JSON.stringify(rules);
  _detectSettingsDirty();
}

function _addPiiRule() {
  const raw = document.getElementById('set_pii_custom_rules').value;
  let rules = [];
  try { rules = JSON.parse(raw); } catch (e) {}
  if (!Array.isArray(rules)) rules = [];
  rules.push({name: '', entity: '', pattern: '', action: 'mask'});
  document.getElementById('set_pii_custom_rules').value = JSON.stringify(rules);
  _renderPiiCustomRules();
  _detectSettingsDirty();
}

function _removePiiRule(idx) {
  const raw = document.getElementById('set_pii_custom_rules').value;
  let rules = [];
  try { rules = JSON.parse(raw); } catch (e) {}
  if (!Array.isArray(rules)) rules = [];
  rules.splice(idx, 1);
  document.getElementById('set_pii_custom_rules').value = JSON.stringify(rules);
  _renderPiiCustomRules();
  _detectSettingsDirty();
}

async function _runPiiTest() {
  const input = document.getElementById('pii_test_input').value;
  const output = document.getElementById('pii_test_output');
  const meta = document.getElementById('pii_test_meta');
  output.value = '';
  meta.textContent = '';
  const settings = {
    pii_filter_enabled: document.getElementById('set_pii_filter_enabled').checked,
    pii_preset_phone: document.getElementById('set_pii_preset_phone').checked,
    pii_preset_id_card: document.getElementById('set_pii_preset_id_card').checked,
    pii_preset_email: document.getElementById('set_pii_preset_email').checked,
    pii_preset_bank_card: document.getElementById('set_pii_preset_bank_card').checked,
    pii_preset_phone_action: document.getElementById('set_pii_preset_phone_action').value,
    pii_preset_id_card_action: document.getElementById('set_pii_preset_id_card_action').value,
    pii_preset_email_action: document.getElementById('set_pii_preset_email_action').value,
    pii_preset_bank_card_action: document.getElementById('set_pii_preset_bank_card_action').value,
    pii_custom_rules: document.getElementById('set_pii_custom_rules').value,
  };
  try {
    // 统一走 admin.js 全局 fetch 包装（自动注入 CSRF / 401 / 403 重试）
    const resp = await fetch('/admin/pii-filter/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({text: input, settings}),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(extractDetailMessage(data.detail) || 'HTTP ' + resp.status);
    output.value = data.text;
    meta.textContent = 'action=' + (data.action || 'none') + ' triggered=' + (data.triggered.map(t => t.entity).join(',') || '-');
  } catch (err) {
    meta.textContent = 'Error: ' + err.message;
  }
}

// PII 总开关：切换时先回滚 checkbox 并弹出确认框，确认后才真正应用新值。
// 这样即使通过 backdrop/X 等途径关闭确认框，都等效于取消，不会留下未确认的新状态。
let _piiTogglePending = null;

function _openPiiToggleConfirm() {
  const enabling = _piiTogglePending;
  const title = enabling ? I18n.t('settings.piiConfirmEnableTitle') : I18n.t('settings.piiConfirmDisableTitle');
  const message = enabling ? I18n.t('settings.piiConfirmEnableMsg') : I18n.t('settings.piiConfirmDisableMsg');
  const btnText = enabling ? I18n.t('settings.piiConfirmEnableBtn') : I18n.t('settings.piiConfirmDisableBtn');
  document.getElementById('piiToggleConfirmTitle').textContent = title;
  document.getElementById('piiToggleConfirmMessage').textContent = message;
  document.getElementById('piiToggleConfirmBtn').textContent = btnText;
  ModalManager.open(document.getElementById('piiToggleConfirmModal'));
}

function confirmPiiToggle() {
  const toggle = document.getElementById('set_pii_filter_enabled');
  if (toggle && _piiTogglePending !== null) {
    toggle.checked = _piiTogglePending;
  }
  ModalManager.close(document.getElementById('piiToggleConfirmModal'));
  _detectSettingsDirty();
}

function closePiiToggleConfirm() {
  // checkbox 在 change 时已回滚为原值，无需额外处理
  ModalManager.close(document.getElementById('piiToggleConfirmModal'));
  _detectSettingsDirty();
}

function _bindPiiFilterUI() {
  const addBtn = document.getElementById('pii_add_rule');
  if (addBtn && addBtn.dataset.bound !== '1') {
    addBtn.dataset.bound = '1';
    addBtn.addEventListener('click', _addPiiRule);
  }
  const testBtn = document.getElementById('pii_test_btn');
  if (testBtn && testBtn.dataset.bound !== '1') {
    testBtn.dataset.bound = '1';
    testBtn.addEventListener('click', _runPiiTest);
  }
  const toggle = document.getElementById('set_pii_filter_enabled');
  if (toggle && toggle.dataset.confirmBound !== '1') {
    toggle.dataset.confirmBound = '1';
    toggle.addEventListener('change', () => {
      _piiTogglePending = toggle.checked;
      // 先回滚，待确认后再应用，避免未确认即生效
      toggle.checked = !_piiTogglePending;
      // 回滚后立即重检，清除通用 change 监听留下的脏标记
      _detectSettingsDirty();
      _openPiiToggleConfirm();
    });
  }
}

Object.assign(window, {
    switchSettingsSection,
    initSettings,
    syncLbStrategyMode,
    loadSettings,
    saveSettings,
    loadFormatConversionPanel,
    confirmPiiToggle,
    closePiiToggleConfirm,
});

// Tab 生命周期：片段 settle 后初始化设置页（含事件绑定、默认 section、数据加载）。
window.TabRuntime.register('settings', {
    init() {
        if (!document.getElementById('set_host') && !document.getElementById('settings_server')) return;
        initSettings();
        switchSettingsSection('server');
        loadSettings();
    },
});

// 轻量预加载：仅拉取并缓存 settings，不依赖设置页已渲染，
// 让统计页等无需先进入设置 Tab 即可拿到 aggregation_timezone 等配置。
async function preloadSettings() {
    try {
        const resp = await fetch('/admin/settings');
        if (!resp.ok) return;
        _settingsOriginal = await resp.json();
    } catch (e) {
        // 预加载失败不影响主流程，统计页时区显示退回浏览器本地时区
    }
}

window.adminSettings = { getOriginal: getOriginalSettings, preload: preloadSettings };
})();
