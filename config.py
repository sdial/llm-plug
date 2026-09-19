import asyncio
import json
import os
from typing import Literal, TypedDict

from loguru import logger

from atomic_json import write_json_atomic

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

ConfigValueType = Literal["str", "int", "float", "bool"]
ConfigValue = str | int | float | bool


class _ConfigSchemaRequired(TypedDict):
    type: ConfigValueType
    default: ConfigValue
    requires_restart: bool


class ConfigSchemaEntry(_ConfigSchemaRequired, total=False):
    readonly: bool


_CONFIG_SCHEMA: dict[str, ConfigSchemaEntry] = {
    "host": {
        "type": "str",
        "default": "0.0.0.0",
        "requires_restart": True,
        "readonly": True,
    },
    "port": {
        "type": "int",
        "default": 55555,
        "requires_restart": True,
        "readonly": True,
    },
    "request_timeout": {"type": "int", "default": 120, "requires_restart": False},
    "max_body_size": {
        "type": "int",
        "default": 20 * 1024 * 1024,
        "requires_restart": False,
    },
    "stats_sqlite_path": {
        "type": "str",
        "default": os.path.join(DATA_DIR, "stats.db"),
        "requires_restart": False,
    },
    "request_log_sqlite_path": {
        "type": "str",
        "default": os.path.join(DATA_DIR, "request_logs.db"),
        "requires_restart": False,
        # 设置页一直只读展示（ADR-0018 票03 渲染等价基线）：登记进 schema 让描述
        # 端点成为唯一属主，PUT 侧同步跳过（历史行为：UI 从不可编辑、从不保存此键）
        "readonly": True,
    },
    "save_request_headers": {
        "type": "bool",
        "default": True,
        "requires_restart": False,
    },
    "save_response_headers": {
        "type": "bool",
        "default": True,
        "requires_restart": False,
    },
    "save_request_body": {
        "type": "bool",
        "default": True,
        "requires_restart": False,
    },
    "save_response_body": {
        "type": "bool",
        "default": True,
        "requires_restart": False,
    },
    "save_files": {
        "type": "bool",
        "default": True,
        "requires_restart": False,
    },
    "save_images": {
        "type": "bool",
        "default": True,
        "requires_restart": False,
    },
    "save_audios": {
        "type": "bool",
        "default": True,
        "requires_restart": False,
    },
    "max_log_body_size": {
        "type": "int",
        "default": 0,
        "requires_restart": False,
    },
    "max_stream_chunks": {
        "type": "int",
        "default": 50000,
        "requires_restart": False,
    },
    "allow_format_conversion": {
        "type": "bool",
        "default": True,
        "requires_restart": False,
    },
    "max_fail_count": {"type": "int", "default": 3, "requires_restart": False},
    "cooldown_seconds": {"type": "int", "default": 120, "requires_restart": False},
    "group_probe_interval_seconds": {"type": "int", "default": 60, "requires_restart": False},
    "group_probe_concurrency": {"type": "int", "default": 5, "requires_restart": False},
    "group_probe_timeout": {"type": "int", "default": 10, "requires_restart": False},
    "rate_limit_wait_seconds": {
        "type": "int",
        "default": 30,
        "requires_restart": False,
    },
    "lb_strategy": {"type": "str", "default": "round_robin", "requires_restart": False},
    "sticky_ttl": {"type": "int", "default": 1800, "requires_restart": False},
    "sticky_cache_max_entries": {
        "type": "int",
        "default": 10000,
        "requires_restart": False,
    },
    "response_state_max_entries": {
        "type": "int",
        "default": 1000,
        "requires_restart": False,
    },
    "response_state_ttl_minutes": {
        "type": "int",
        "default": 60,
        "requires_restart": False,
    },
    "response_state_cleanup_interval_minutes": {
        "type": "int",
        "default": 30,
        "requires_restart": False,
    },
    "aggregation_timezone": {"type": "str", "default": "", "requires_restart": False},
    "request_log_retention_days": {
        "type": "int",
        "default": 7,
        "requires_restart": False,
    },
    "request_log_raw_retention_days": {
        "type": "int",
        "default": 1,
        "requires_restart": False,
    },
    "admin_max_attempts": {
        "type": "int",
        "default": 10,
        "requires_restart": False,
    },
    "admin_lockout_base_seconds": {
        "type": "int",
        "default": 60,
        "requires_restart": False,
    },
    "context_shaping_strip_ansi": {
        "type": "bool",
        "default": True,
        "requires_restart": False,
    },
    "context_shaping_trim_trailing_whitespace": {"type": "bool", "default": False, "requires_restart": False},
    "context_shaping_collapse_blank_lines": {"type": "bool", "default": False, "requires_restart": False},
    "context_shaping_dedupe_consecutive_lines": {"type": "bool", "default": False, "requires_restart": False},
    "context_shaping_dedupe_adjacent_user_messages": {"type": "bool", "default": False, "requires_restart": False},
    "context_shaping_strip_unreferenced_tool_results": {"type": "bool", "default": False, "requires_restart": False},
    "context_shaping_caveman_enabled": {"type": "bool", "default": False, "requires_restart": False},
    "context_shaping_custom_prompt_enabled": {"type": "bool", "default": False, "requires_restart": False},
    "context_shaping_custom_prompt_text": {"type": "str", "default": "", "requires_restart": False},
    "context_shaping_custom_prompt_version": {"type": "int", "default": 0, "requires_restart": False, "readonly": True},
    "pii_filter_enabled": {
        "type": "bool",
        "default": False,
        "requires_restart": False,
    },
    "pii_preset_phone": {
        "type": "bool",
        "default": True,
        "requires_restart": False,
    },
    "pii_preset_id_card": {
        "type": "bool",
        "default": True,
        "requires_restart": False,
    },
    "pii_preset_email": {
        "type": "bool",
        "default": True,
        "requires_restart": False,
    },
    "pii_preset_bank_card": {
        "type": "bool",
        "default": True,
        "requires_restart": False,
    },
    "pii_custom_rules": {
        "type": "str",
        "default": "[]",
        "requires_restart": False,
    },
    "pii_exempt_channels": {
        "type": "str",
        "default": "[]",
        "requires_restart": False,
    },
    "pii_preset_phone_action": {
        "type": "str",
        "default": "mask",
        "requires_restart": False,
    },
    "pii_preset_id_card_action": {
        "type": "str",
        "default": "mask",
        "requires_restart": False,
    },
    "pii_preset_email_action": {
        "type": "str",
        "default": "mask",
        "requires_restart": False,
    },
    "pii_preset_bank_card_action": {
        "type": "str",
        "default": "mask",
        "requires_restart": False,
    },
}

_settings: dict = {}
_settings_lock = asyncio.Lock()


HOST = _CONFIG_SCHEMA["host"]["default"]
PORT = _CONFIG_SCHEMA["port"]["default"]

CHANNELS_FILE = os.path.join(DATA_DIR, "channels.json")
API_KEYS_FILE = os.path.join(DATA_DIR, "api_keys.json")
ADMIN_AUTH_FILE = os.path.join(DATA_DIR, "admin_auth.json")

REQUEST_TIMEOUT = _CONFIG_SCHEMA["request_timeout"]["default"]
MAX_BODY_SIZE = _CONFIG_SCHEMA["max_body_size"]["default"]

LOG_LEVEL = "info"  # 仅通过 --log-level CLI 参数设置

_CONFIG_CONSTRAINTS: dict[str, dict] = {
    "request_timeout": {"min": 1, "max": 3600},
    "max_body_size": {"min": 1024, "max": 1024 * 1024 * 1024},
    "max_log_body_size": {"min": 0, "max": 256 * 1024 * 1024},
    "max_stream_chunks": {"min": 100, "max": 100000},
    "max_fail_count": {"min": 1, "max": 100000},
    "cooldown_seconds": {"min": 1, "max": 86400},
    "group_probe_interval_seconds": {"min": 1, "max": 86400},
    "group_probe_concurrency": {"min": 1, "max": 100},
    "group_probe_timeout": {"min": 1, "max": 300},
    "rate_limit_wait_seconds": {"min": 0, "max": 300},
    "lb_strategy": {"choices": ("round_robin", "backup", "sticky")},
    "sticky_ttl": {"min": 60, "max": 86400},
    "sticky_cache_max_entries": {"min": 100, "max": 1000000},
    "response_state_max_entries": {"min": 1, "max": 10_000_000},
    "response_state_ttl_minutes": {"min": 1, "max": 525600},
    "response_state_cleanup_interval_minutes": {"min": 1, "max": 1440},
    "aggregation_timezone": {"validator": "iana_timezone"},
    "request_log_retention_days": {"min": 0},
    "request_log_raw_retention_days": {"min": 0},
    "admin_max_attempts": {"min": 1, "max": 100},
    "admin_lockout_base_seconds": {"min": 10, "max": 86400},
    "pii_preset_phone_action": {"choices": ("mask", "replace", "block")},
    "pii_preset_id_card_action": {"choices": ("mask", "replace", "block")},
    "pii_preset_email_action": {"choices": ("mask", "replace", "block")},
    "pii_preset_bank_card_action": {"choices": ("mask", "replace", "block")},
}


# 设置页 UI 元数据（ADR-0018 D0）：与 schema/约束平行的一份每键登记，由字段描述
# 端点投影给前端绑定器与第二期 schema 渲染。键顺序 = 描述输出顺序 = 分区内渲染顺序。
#   section   设置页分区（server/request/format_conversion/lb/timezone/database/
#             security/pii-filter/context_shaping）；hidden＝不在设置页渲染
#   group     分区内挂载槽序号（对应片段里的 data-schema-group 挂载点）
#   label_key 必填 i18n 文案键；help_key/suffix_key 可选
#   unit      显示单位（绑定器内置 MB/KB ↔ 字节换算）；min/max 权威值保持 wire 刻度
#   hot       label 旁渲染"热更新" pill
#   trim      字符串键读取时去首尾空白（仅存量键显式声明，不全局启用）
#   choice_label_keys  choices 值 → 选项文案键
_CONFIG_UI_META: dict[str, dict] = {
    "host": {"section": "server", "group": 0, "label_key": "settings.hostLabel"},
    "port": {"section": "server", "group": 0, "label_key": "settings.portLabel"},
    "request_timeout": {
        "section": "request",
        "group": 0,
        "label_key": "settings.timeoutLabel",
        "help_key": "settings.timeoutHelp",
    },
    "max_body_size": {"section": "request", "group": 0, "label_key": "settings.maxBodyLabel", "unit": "MB"},
    "max_stream_chunks": {
        "section": "request",
        "group": 1,
        "label_key": "settings.streamChunksLabel",
        "help_key": "settings.streamChunksHelp1",
        "help_class": "text-xs text-ink-500 mt-2",
        "hot": True,
    },
    "lb_strategy": {
        "section": "lb",
        "group": 0,
        "label_key": "settings.lbStrategyLabel",
        "choice_label_keys": {
            "round_robin": "settings.lbStrategyRoundRobin",
            "backup": "settings.lbStrategyBackup",
            "sticky": "settings.lbStrategySticky",
        },
        # 选项说明行（动态 help 槽）按选中项取键，等价于手写版 syncLbStrategyMode 的长文案
        "choice_help_keys": {
            "round_robin": "settings.lbStrategyHelpRR",
            "backup": "settings.lbStrategyHelpBackup",
            "sticky": "settings.lbStrategyHelpSticky",
        },
    },
    "sticky_ttl": {"section": "lb", "group": 1, "label_key": "settings.stickyTtlLabel"},
    "sticky_cache_max_entries": {"section": "lb", "group": 1, "label_key": "settings.stickyCacheLabel"},
    "max_fail_count": {
        "section": "lb",
        "group": 2,
        "label_key": "settings.maxFailLabel",
        "help_key": "settings.maxFailHelp",
    },
    "cooldown_seconds": {
        "section": "lb",
        "group": 2,
        "label_key": "settings.cooldownLabel",
        "help_key": "settings.cooldownHelp",
    },
    "rate_limit_wait_seconds": {
        "section": "lb",
        "group": 2,
        "label_key": "settings.rateLimitWaitLabel",
        "help_key": "settings.rateLimitWaitHelp",
    },
    "group_probe_interval_seconds": {
        "section": "lb",
        "group": 3,
        "label_key": "settings.groupProbeIntervalLabel",
        "help_key": "settings.groupProbeIntervalHelp",
    },
    "group_probe_concurrency": {
        "section": "lb",
        "group": 3,
        "label_key": "settings.groupProbeConcurrencyLabel",
        "help_key": "settings.groupProbeConcurrencyHelp",
    },
    "group_probe_timeout": {
        "section": "lb",
        "group": 3,
        "label_key": "settings.groupProbeTimeoutLabel",
        "help_key": "settings.groupProbeTimeoutHelp",
    },
    "aggregation_timezone": {
        "section": "timezone",
        "group": 0,
        "label_key": "settings.tzLabel",
        "help_key": "settings.tzHelp",
        "hot": True,
        "trim": True,
        "blank_option_key": "settings.tzSelectBlank",
    },
    "request_log_sqlite_path": {
        "section": "database",
        "group": 0,
        "label_key": "settings.dbSqliteLabel",
        "input_class": "text-ink-500 font-mono",
    },
    "save_request_headers": {"section": "database", "group": 1, "label_key": "settings.dbSaveReqHeaders"},
    "save_response_headers": {"section": "database", "group": 1, "label_key": "settings.dbSaveRespHeaders"},
    "save_request_body": {"section": "database", "group": 1, "label_key": "settings.dbSaveReqBody"},
    "save_response_body": {"section": "database", "group": 1, "label_key": "settings.dbSaveRespBody"},
    "save_files": {"section": "database", "group": 1, "label_key": "settings.dbSaveFiles"},
    "save_images": {"section": "database", "group": 1, "label_key": "settings.dbSaveImages"},
    "save_audios": {"section": "database", "group": 1, "label_key": "settings.dbSaveAudios"},
    "max_log_body_size": {
        "section": "database",
        "group": 2,
        "label_key": "settings.dbTruncLabel",
        "help_key": "settings.dbTruncUnit",
        "unit": "KB",
        "hot": True,
    },
    "request_log_raw_retention_days": {
        "section": "database",
        "group": 3,
        "label_key": "settings.dbRawRetentionLabel",
        "label_class": "text-xs font-medium text-ink-700 block mb-1",
        "suffix_key": "settings.dbDaysUnit",
    },
    "request_log_retention_days": {
        "section": "database",
        "group": 3,
        "label_key": "settings.dbFullRetentionLabel",
        "label_class": "text-xs font-medium text-ink-700 block mb-1",
        "suffix_key": "settings.dbDaysUnit",
    },
    "admin_max_attempts": {"section": "security", "group": 0, "label_key": "settings.secMaxAttemptsLabel"},
    "admin_lockout_base_seconds": {
        "section": "security",
        "group": 0,
        "label_key": "settings.secLockoutBaseLabel",
    },
    "stats_sqlite_path": {"section": "hidden", "group": 0, "label_key": "settings.statsSqliteLabel"},
    "response_state_max_entries": {
        "section": "hidden",
        "group": 0,
        "label_key": "settings.responseStateMaxEntriesLabel",
    },
    "response_state_ttl_minutes": {
        "section": "hidden",
        "group": 0,
        "label_key": "settings.responseStateTtlLabel",
    },
    "response_state_cleanup_interval_minutes": {
        "section": "hidden",
        "group": 0,
        "label_key": "settings.responseStateCleanupLabel",
    },
    "allow_format_conversion": {
        "section": "format_conversion",
        "group": 0,
        "label_key": "settings.fcGlobalDefault",
    },
    "context_shaping_strip_ansi": {
        "section": "context_shaping",
        "group": 0,
        "label_key": "contextShaping.stripAnsi",
    },
    "context_shaping_trim_trailing_whitespace": {"section": "context_shaping", "group": 0, "label_key": "contextShaping.trimWhitespace"},
    "context_shaping_collapse_blank_lines": {"section": "context_shaping", "group": 0, "label_key": "contextShaping.collapseBlankLines"},
    "context_shaping_dedupe_consecutive_lines": {"section": "context_shaping", "group": 0, "label_key": "contextShaping.dedupeLines"},
    "context_shaping_dedupe_adjacent_user_messages": {"section": "context_shaping", "group": 0, "label_key": "contextShaping.dedupeUsers"},
    "context_shaping_strip_unreferenced_tool_results": {
        "section": "context_shaping",
        "group": 0,
        "label_key": "contextShaping.stripUnreferencedTools",
    },
    "context_shaping_caveman_enabled": {"section": "context_shaping", "group": 1, "label_key": "contextShaping.caveman"},
    "context_shaping_custom_prompt_enabled": {"section": "context_shaping", "group": 1, "label_key": "contextShaping.customEnabled"},
    "context_shaping_custom_prompt_text": {"section": "context_shaping", "group": 1, "label_key": "contextShaping.customText"},
    "context_shaping_custom_prompt_version": {"section": "context_shaping", "group": 1, "label_key": "contextShaping.customVersion"},
    "pii_filter_enabled": {"section": "pii-filter", "group": 0, "label_key": "settings.piiTitle"},
    "pii_preset_phone": {"section": "pii-filter", "group": 0, "label_key": "settings.piiPresetPhone"},
    "pii_preset_phone_action": {
        "section": "pii-filter",
        "group": 0,
        "label_key": "settings.piiActionLabel",
        "choice_label_keys": {
            "mask": "settings.piiActionMask",
            "replace": "settings.piiActionReplace",
            "block": "settings.piiActionBlock",
        },
    },
    "pii_preset_id_card": {"section": "pii-filter", "group": 0, "label_key": "settings.piiPresetIdCard"},
    "pii_preset_id_card_action": {
        "section": "pii-filter",
        "group": 0,
        "label_key": "settings.piiActionLabel",
        "choice_label_keys": {
            "mask": "settings.piiActionMask",
            "replace": "settings.piiActionReplace",
            "block": "settings.piiActionBlock",
        },
    },
    "pii_preset_email": {"section": "pii-filter", "group": 0, "label_key": "settings.piiPresetEmail"},
    "pii_preset_email_action": {
        "section": "pii-filter",
        "group": 0,
        "label_key": "settings.piiActionLabel",
        "choice_label_keys": {
            "mask": "settings.piiActionMask",
            "replace": "settings.piiActionReplace",
            "block": "settings.piiActionBlock",
        },
    },
    "pii_preset_bank_card": {"section": "pii-filter", "group": 0, "label_key": "settings.piiPresetBankCard"},
    "pii_preset_bank_card_action": {
        "section": "pii-filter",
        "group": 0,
        "label_key": "settings.piiActionLabel",
        "choice_label_keys": {
            "mask": "settings.piiActionMask",
            "replace": "settings.piiActionReplace",
            "block": "settings.piiActionBlock",
        },
    },
    "pii_custom_rules": {"section": "pii-filter", "group": 1, "label_key": "settings.piiCustom"},
    "pii_exempt_channels": {
        "section": "hidden",
        "group": 0,
        "label_key": "settings.piiExemptChannelsLabel",
    },
}


def _validate_pii_custom_rules(value: str):
    try:
        rules = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"pii_custom_rules 不是合法 JSON: {exc}") from exc
    if not isinstance(rules, list):
        raise ValueError("pii_custom_rules 必须是 JSON 数组")
    for idx, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"pii_custom_rules[{idx}] 必须是对象")
        if "entity" not in rule or "pattern" not in rule:
            raise ValueError(f"pii_custom_rules[{idx}] 缺少 entity 或 pattern")
        # 校验正则是否可编译
        try:
            import re

            re.compile(rule["pattern"])
        except re.error as exc:
            raise ValueError(f"pii_custom_rules[{idx}].pattern 正则编译失败: {exc}") from exc


def _validate_pii_settings(settings: dict):
    if "pii_custom_rules" in settings:
        _validate_pii_custom_rules(settings["pii_custom_rules"])


def _validate_iana_timezone(value: str):
    if not value:
        return
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(value)
    except (
        ZoneInfoNotFoundError,
        ValueError,
        PermissionError,
        IsADirectoryError,
    ) as exc:
        raise ValueError(f"aggregation_timezone 不是有效的 IANA 时区名: {value!r}") from exc


def _validate_setting(key: str, value):
    constraints = _CONFIG_CONSTRAINTS.get(key)
    if not constraints:
        return
    if "min" in constraints and value < constraints["min"]:
        raise ValueError(f"{key} must be >= {constraints['min']}, got {value}")
    if "max" in constraints and value > constraints["max"]:
        raise ValueError(f"{key} must be <= {constraints['max']}, got {value}")
    if "choices" in constraints and str(value).lower() not in constraints["choices"]:
        raise ValueError(f"{key} must be one of {constraints['choices']}, got {value!r}")
    validator = constraints.get("validator")
    if validator == "iana_timezone":
        _validate_iana_timezone(value)


def _cast_value(value, type_name):
    if type_name == "int":
        return int(value)
    if type_name == "float":
        return float(value)
    if type_name == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    return str(value)


def _init_settings_sync():
    global _settings
    file_data = {}
    if os.path.exists(_SETTINGS_FILE):
        try:
            with open(_SETTINGS_FILE, encoding="utf-8") as f:
                file_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning(f"Failed to read {_SETTINGS_FILE}, using defaults")
    legacy_existing_file_defaults = {}
    if os.path.exists(_SETTINGS_FILE):
        legacy_existing_file_defaults = {
            "request_log_retention_days": 0,
            "request_log_raw_retention_days": 0,
        }
    _settings = {}
    for key, schema in _CONFIG_SCHEMA.items():
        if key in file_data:
            try:
                value = _cast_value(file_data[key], schema["type"])
                _validate_setting(key, value)
            except (TypeError, ValueError) as exc:
                logger.warning("Invalid persisted setting %s; using default: %s", key, exc)
                value = legacy_existing_file_defaults.get(key, schema["default"])
            _settings[key] = value
        else:
            _settings[key] = legacy_existing_file_defaults.get(key, schema["default"])
    try:
        _validate_pii_settings(_settings)
        _validate_context_shaping_settings(_settings)
    except ValueError as exc:
        logger.warning("Invalid persisted coupled settings; resetting the affected settings: %s", exc)
        for key in (
            "pii_custom_rules",
            "context_shaping_custom_prompt_enabled",
            "context_shaping_custom_prompt_text",
            "context_shaping_custom_prompt_version",
        ):
            _settings[key] = _CONFIG_SCHEMA[key]["default"]
    _sync_module_vars()


def _sync_module_vars():
    global HOST, PORT, REQUEST_TIMEOUT, MAX_BODY_SIZE
    HOST = _settings.get("host", "0.0.0.0")
    PORT = _settings.get("port", 55555)
    REQUEST_TIMEOUT = _settings.get("request_timeout", 600)
    MAX_BODY_SIZE = _settings.get("max_body_size", 20 * 1024 * 1024)


def get_setting(key: str):
    if key in _settings:
        return _settings[key]
    schema = _CONFIG_SCHEMA.get(key)
    if schema:
        return schema["default"]
    return None


def get_settings() -> dict:
    return dict(_settings)


async def _apply_lb_settings():
    try:
        from proxy import outcomes as _outcomes

        _outcomes.configure(
            max_fail_count=_settings.get("max_fail_count", 3),
            cooldown_seconds=_settings.get("cooldown_seconds", 120),
        )
        from balancer.load_balancer import load_balancer

        await load_balancer.update_config(
            strategy=_settings.get("lb_strategy", "round_robin"),
            sticky_ttl=_settings.get("sticky_ttl", 1800),
            sticky_cache_max_entries=_settings.get("sticky_cache_max_entries", 10000),
        )
    except Exception:
        logger.warning(
            "Failed to apply LB settings, load balancer will use previous config",
            exc_info=True,
        )


def _save_settings_to_disk_sync():
    """同步写入 settings.json（原子写）"""
    write_json_atomic(_SETTINGS_FILE, _settings, temp_prefix=".settings_")


async def _save_settings_to_disk():
    await asyncio.to_thread(_save_settings_to_disk_sync)


async def _migrate_lb_config():
    """经 Channel Catalog 迁移旧 ``channels.json.lb_config``。"""
    from channel_catalog import catalog

    lb_config = await catalog.take_legacy_lb_config()
    if lb_config:
        if "max_fail_count" in lb_config and _settings.get("max_fail_count", 3) == 3:
            try:
                value = _cast_value(lb_config["max_fail_count"], _CONFIG_SCHEMA["max_fail_count"]["type"])
                _validate_setting("max_fail_count", value)
                _settings["max_fail_count"] = value
            except (TypeError, ValueError) as exc:
                logger.warning("Ignoring invalid legacy max_fail_count: %s", exc)
        if "cooldown_seconds" in lb_config and _settings.get("cooldown_seconds", 120) == 120:
            try:
                value = _cast_value(lb_config["cooldown_seconds"], _CONFIG_SCHEMA["cooldown_seconds"]["type"])
                _validate_setting("cooldown_seconds", value)
                _settings["cooldown_seconds"] = value
            except (TypeError, ValueError) as exc:
                logger.warning("Ignoring invalid legacy cooldown_seconds: %s", exc)
    await asyncio.to_thread(_save_settings_to_disk_sync)


async def init_settings():
    _init_settings_sync()
    await _migrate_lb_config()
    await _apply_lb_settings()


def _probe_interval_soft_warnings(updated_keys: list[str]) -> list[str]:
    """软约束：保持不变量 `group_probe_interval_seconds ≤ cooldown_seconds`。

    仅当本次更新触及探活间隔或冷却期时才评估——否则每次无关保存都会对历史违反
    状态重复告警。违反时允许保存（硬拒绝会锁死有意调大 cooldown 的用户），只返回
    warning 文本供调用方/前端呈现。
    """
    if not ({"group_probe_interval_seconds", "cooldown_seconds"} & set(updated_keys)):
        return []
    interval = get_setting("group_probe_interval_seconds")
    cooldown = get_setting("cooldown_seconds")
    if interval is not None and cooldown is not None and interval > cooldown:
        message = (
            f"group_probe_interval_seconds({interval}) > cooldown_seconds({cooldown}): "
            "探活间隔超出冷却期，降级对可能在两次探活之间因冷却到期裸奔进业务轮换"
        )
        logger.warning(message)
        return [message]
    return []


def _validate_context_shaping_settings(settings: dict[str, object]) -> None:
    text = str(settings.get("context_shaping_custom_prompt_text", ""))
    if len(text.encode("utf-8")) > 32768:
        raise ValueError("context_shaping_custom_prompt_text exceeds 32768 UTF-8 bytes")
    if settings.get("context_shaping_custom_prompt_enabled") and not text.strip():
        raise ValueError("context_shaping_custom_prompt_text must not be blank when enabled")


async def update_settings(updates: dict) -> dict:
    global _settings
    updated_keys = []
    needs_restart = False
    staged: dict[str, object] = {}
    async with _settings_lock:
        for key, value in updates.items():
            schema = _CONFIG_SCHEMA.get(key)
            if schema is None:
                continue
            if schema.get("readonly"):
                continue
            try:
                casted = _cast_value(value, schema["type"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} type cast failed: {exc}") from exc
            _validate_setting(key, casted)
            staged[key] = casted
            updated_keys.append(key)
            if schema.get("requires_restart"):
                needs_restart = True
        validated = {**_settings, **staged}
        _validate_pii_settings(validated)
        _validate_context_shaping_settings(validated)
        if "context_shaping_custom_prompt_text" in staged and staged["context_shaping_custom_prompt_text"] != _settings.get(
            "context_shaping_custom_prompt_text", ""
        ):
            staged["context_shaping_custom_prompt_version"] = int(_settings.get("context_shaping_custom_prompt_version", 0)) + 1
            updated_keys.append("context_shaping_custom_prompt_version")
        if updated_keys:
            previous_settings = _settings
            _settings = {**previous_settings, **staged}
            try:
                await _save_settings_to_disk()
            except Exception:
                # 磁盘写入是设置变更的提交点；失败时内存必须保持与持久化状态一致。
                _settings = previous_settings
                raise
            _sync_module_vars()
    await _apply_lb_settings()
    warnings = _probe_interval_soft_warnings(updated_keys)
    # 如果 request_timeout 变更，清理客户端缓存以应用新超时
    if "request_timeout" in updated_keys:
        try:
            from client import invalidate_all_clients

            await invalidate_all_clients()
        except Exception as e:
            logger.warning(f"Failed to invalidate clients after timeout change: {e}")
    if any(key.startswith("response_state_") for key in updated_keys):
        try:
            from response_state import reload_responses_store

            reload_responses_store()
        except Exception as e:
            logger.warning(f"Failed to reload response state store after settings change: {e}")
    return {"updated": updated_keys, "needs_restart": needs_restart, "warnings": warnings}
