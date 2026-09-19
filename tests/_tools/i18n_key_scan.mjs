import { readFileSync, readdirSync, statSync } from 'node:fs';

const dicts = {};
globalThis.I18n = { registerDict: (lang, d) => { dicts[lang] = d; } };
globalThis.window = globalThis;

for (const f of ['static/js/i18n-zh.js', 'static/js/i18n-en.js']) {
  eval(readFileSync(f, 'utf-8'));
}
function flatten(d, prefix, out) {
  for (const [k, v] of Object.entries(d)) {
    const fk = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === 'object') flatten(v, fk, out);
    else out.add(fk);
  }
}
const zh = new Set(); flatten(dicts.zh, '', zh);
const en = new Set(); flatten(dicts.en, '', en);

function collect(dir, acc) {
  for (const name of readdirSync(dir)) {
    const p = `${dir}/${name}`;
    if (statSync(p).isDirectory()) collect(p, acc);
    else if (/\.(js|html)$/.test(name)) acc.push(p);
  }
}
const files = [];
collect('static', files);

const dataKeys = new Set();
const tKeys = new Set();
const ATTRS = ['data-i18n', 'data-i18n-html', 'data-i18n-placeholder', 'data-i18n-title', 'data-i18n-aria'];
for (const f of files) {
  const text = readFileSync(f, 'utf-8');
  for (const attr of ATTRS) {
    const re = new RegExp(`${attr}="([^"]+)"`, 'g');
    let m;
    while ((m = re.exec(text))) {
      // ADR-0018 D2：渲染器按描述动态拼 data-i18n 属性（如 data-i18n="' + k + '"），
      // 捕获值含引号/加号的是拼接产物而非字面键——描述文案键由 config.py 扫描覆盖。
      if (!/['"+]/.test(m[1])) dataKeys.add(m[1]);
    }
  }
  const re = /I18n\.t\(\s*['"]([^'"]+)['"]/g;
  let m;
  while ((m = re.exec(text))) tKeys.add(m[1]);
  // 绑定别名：const tokens = I18n.t.bind(I18n) 后用 tokens('key')
  const re2 = /\btokens?\(\s*['"]([^'"]+)['"]/g;
  while ((m = re2.exec(text))) tKeys.add(m[1]);
  // data-i18n 或 translateRoot 内的任意 translateElement 键也覆盖（属性已捕获）
}
// config 侧 UI 元数据（ADR-0018 D0）里的文案键引用（label_key/help_key/suffix_key/
// choice_label_keys 等的值）同样算"已使用"：Python 侧登记的键必须入典，否则死键
// 检查会误杀。提取点分字符串后按扩展名排除文件名（settings.json 之类）。
try {
  const configSrc = readFileSync('config.py', 'utf-8');
  const cfgRe = /['"]((?:settings|contextShaping)\.[A-Za-z0-9_.]+)['"]/g;
  const fileExt = /\.(json|db|csv|py|jsonl|html|js|css|md|bak|tmp)$/i;
  let cm;
  while ((cm = cfgRe.exec(configSrc))) {
    if (!fileExt.test(cm[1])) tKeys.add(cm[1]);
  }
} catch {}

const dynamicKeys = new Set([
  'streamTest.statusReady', 'streamTest.statusStreaming', 'streamTest.statusDone', 'streamTest.statusError', 'streamTest.statusStopped',
  // requests.js resolves Context Shaping feature names from a fixed feature-to-key map.
  'requests.shapingFeature.stripAnsi', 'requests.shapingFeature.trimTrailingWhitespace', 'requests.shapingFeature.collapseBlankLines',
  'requests.shapingFeature.dedupeConsecutiveLines', 'requests.shapingFeature.stripUnreferencedToolResults',
  'requests.shapingFeature.dedupeAdjacentUserMessages', 'requests.shapingFeature.cavemanPromptExtension',
  'requests.shapingFeature.customPromptExtension',
]);
const used = new Set([...dataKeys, ...tKeys, ...dynamicKeys]);

const missingZh = [...used].filter(k => !zh.has(k)).sort();
const missingEn = [...used].filter(k => !en.has(k)).sort();
const dead = [...zh].filter(k => !used.has(k)).sort();

console.log(JSON.stringify({
  dictZh: zh.size,
  dictEn: en.size,
  used: used.size,
  missingZh,
  missingEn,
  dead,
}));
