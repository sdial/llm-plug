#!/usr/bin/env python
"""从 i18n-zh.js / i18n-en.js 删除已确认的死键（无任何引用、或仅作为前缀父键）。

用法：uv run python tests/_tools/prune_i18n_dead_keys.py
"""

import re
import sys
from pathlib import Path

STATIC_JS = Path("static/js")
DICT_FILES = ["i18n-zh.js", "i18n-en.js"]

DEAD_KEYS = [
    "analyzer.diagConsecutiveRole",
    "analyzer.diagEmptyContent",
    "common.add",
    "common.all",
    "common.confirm",
    "common.days",
    "common.no",
    "common.none",
    "common.notes",
    "common.processing",
    "common.yes",
    "modals.anthropicBeta",
    "modals.anthropicBetaPh",
    "modals.anthropicVersion",
    "modals.apiKey",
    "modals.baseUrl",
    "modelGroups.channelLabel",
    "modelGroups.nameRequired",
    "requests.filterEnd",
    "requests.filterStart",
    "sessionViewer.noDesc",
    "settings.dbDesc2",
    "settings.dbDiagramClearRaw",
    "settings.dbDiagramDeleteRow",
    "settings.dbDiagramNote",
    "settings.dbDiagramTitle",
    "settings.dbDiagramWrite",
    "settings.dbFullRetentionHelp",
    "settings.dbRawHint",
    "settings.dbRawReqBody",
    "settings.dbRawReqBodyDesc",
    "settings.dbRawReqHeaders",
    "settings.dbRawReqHeadersDesc",
    "settings.dbRawRespBody",
    "settings.dbRawRespBodyDesc",
    "settings.dbRawRespHeaders",
    "settings.dbRawRespHeadersDesc",
    "settings.dbRawRetentionHelp",
    "settings.dbRetentionHint",
    "settings.dbSqliteHelp",
    "settings.dbTruncHelp1",
    "settings.dbTruncHelp2",
    "settings.fcChannelSaved",
    "settings.hostHelp",
    "settings.lbHint",
    "settings.maxBodyHelp",
    "settings.portHelp",
    "settings.requestHint",
    "settings.secLockoutBaseHelp",
    "settings.secMaxAttemptsHelp",
    "settings.secNewPwdHelp",
    "settings.secTierAttempts",
    "settings.secTierDuration",
    "settings.secTierTable",
    "settings.secTierUnit",
    "settings.stickyCacheHelp",
    "settings.stickyTtlHelp",
    "settings.streamChunksHelp2",
    "stats.cacheHitRate",
    "storage.confirmDefault",
    "storage.opFailed",
    "storage.targetPrefix",
    "streamTest.errorPrefix",
    "streamTest.status",
    "streamTest.subtitle",
    "whitelist.descExample",
    "whitelist.methodsExample",
]

KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_]+):\s*(['\"]).*?\3,?\s*$")


def prune_section(section_lines, subkey, removed):
    """删除 section 内的单个 subkey 行；返回清理后的行列表。"""
    out = []
    for line in section_lines:
        m = KEY_RE.match(line)
        if m and m.group(2) == subkey:
            removed.append(line.strip())
            continue  # 删除该行
        out.append(line)
    return out


def prune_dict(path, dead_keys):
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    # 找出所有 section 行（缩进 4 空格 + name: {）与收尾行（4 空格 + } 或 },）
    section_re = re.compile(r"^    ([A-Za-z0-9_]+): \{$")
    close_re = re.compile(r"^    \},?$")
    sections = {}  # section_name -> [lines]
    current = None
    for line in lines:
        m = section_re.match(line)
        if m:
            current = m.group(1)
            sections[current] = []
        elif current is not None:
            if close_re.match(line):
                current = None
            else:
                sections[current].append(line)

    removed = []
    for full in dead_keys:
        section, subkey = full.split(".", 1)
        if section not in sections:
            print(f"  [WARN] section 缺失: {full}")
            continue
        before = len(sections[section])
        sections[section] = prune_section(sections[section], subkey, removed)
        if len(sections[section]) != before - 1:
            print(f"  [WARN] 未找到或删多行: {full} (before={before} after={len(sections[section])})")

    # 重建文件：保持原始行，仅替换 section 内容
    out_lines = []
    current = None
    for line in lines:
        m = section_re.match(line)
        if m:
            current = m.group(1)
            out_lines.append(line)
        elif current is not None and close_re.match(line):
            out_lines.extend(sections[current])
            out_lines.append(line)
            current = None
        elif current is not None:
            pass  # section 内容由上面重建，跳过原始内容行
        else:
            out_lines.append(line)

    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return removed


def main():
    ok = True
    for fname in DICT_FILES:
        removed = prune_dict(STATIC_JS / fname, DEAD_KEYS)
        missing = [k for k in DEAD_KEYS if k.split(".", 1)[1] not in " ".join(removed)]
        if missing:
            ok = False
            print(f"[FAIL] {fname} 缺失/未删除: {missing}")
        print(f"{fname}: 删除 {len(removed)} 键")

    if not ok:
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
