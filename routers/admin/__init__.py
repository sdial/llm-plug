"""管理后台域路由包：/admin 下各域端点按域拆分。

URL 与响应结构与原单文件 routers/admin.py 完全一致，对外无感知。
"""

# 登录速率限制共享状态：routers.admin 命名空间是对外契约，测试直接访问/重置
_login_attempts: dict[str, list[float]] = {}
_LOGIN_RATE_LIMIT_WINDOW_SECONDS = 86400  # 24小时过期

from fastapi import APIRouter  # noqa: E402

import request_logs  # noqa: E402, F401

from . import (  # noqa: E402
    api_keys,
    auth,
    channels,
    context_shaping,
    logs,
    model_groups,
    pii_filter,
    settings,
    stats,
    storage,
    ui,
    upstream_catalog,
    whitelist,
)
from .api_keys import (  # noqa: E402, F401
    create_api_key,
    delete_api_key,
    get_api_key_value,
    list_api_keys,
    regenerate_api_key,
    update_api_key,
)
from .auth import (  # noqa: E402, F401
    AdminChangePasswordRequest,
    AdminLoginRequest,
    AdminPasswordSetup,
    _check_login_allowed,
    _cleanup_expired_attempts,
    _clear_login_failures,
    _format_duration,
    _get_lockout_seconds,
    _record_login_failure,
)
from .channels import (  # noqa: E402, F401
    FetchModelsRequest,
    create_channel,
    delete_channel,
    fetch_models,
    list_available_models,
    list_channels,
    test_channel,
    toggle_channel,
    update_channel,
)
from .common import (  # noqa: E402, F401
    _ALLOWED_LOG_SUFFIX,
    ADMIN_FRAGMENT_DIR,
    DATA_DIR,
    LOGS_DIR,
    STATIC_DIR,
    WHITELIST_PATH,
    AdminAuthRoute,
    _attach_api_key_names,
    _attach_channel_api_types,
    _client_ip,
    _decorate_request_items,
    _get_api_keys,
    _get_channels,
    _requires_csrf,
    _validate_channel_outbound_urls,
    _validate_csrf_for_request,
    _validate_log_filename,
    _validate_outbound_url,
)
from .logs import (  # noqa: E402, F401
    cleanup_request_logs_endpoint,
    get_log,
    get_request_field_endpoint,
    list_logs,
    list_requests_endpoint,
    request_log_get_request_field,
    request_log_list_requests,
    stats_list_requests,
)
from .model_groups import (  # noqa: E402, F401
    create_model_group,
    delete_model_group_endpoint,
    get_lb_config_endpoint,
    list_model_groups,
    toggle_model_group,
    update_lb_config_endpoint,
    update_model_group_endpoint,
)
from .pii_filter import (  # noqa: E402, F401
    PiiTestRequest,
    PiiTestResponse,
    pii_filter_test,
)
from .settings import (  # noqa: E402, F401
    get_settings_endpoint,
    update_settings_endpoint,
)
from .stats import (  # noqa: E402, F401
    get_stats,
    get_stats_today,
    refresh_daily_stats_endpoint,
    refresh_stats_endpoint,
    trigger_daily_aggregation,
)
from .storage import (  # noqa: E402, F401
    StorageCleanupRequest,
    cleanup_storage,
    get_storage_stats,
    preview_cleanup,
)
from .ui import admin_ui_fragment  # noqa: E402, F401
from .whitelist import (  # noqa: E402, F401
    WhitelistPreviewRequest,
    get_whitelist,
    preview_whitelist,
    update_whitelist,
)

router = APIRouter()
for _sub_router in (
    auth,
    channels,
    context_shaping,
    api_keys,
    model_groups,
    pii_filter,
    settings,
    whitelist,
    stats,
    logs,
    storage,
    upstream_catalog,
    ui,
):
    router.include_router(_sub_router.router)
