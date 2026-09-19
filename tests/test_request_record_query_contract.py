"""跨两侧九条件参数化契约测试（ADR-0017 工单01 / D0）。

stats（聚合明细表）与 request_logs（月度明细表）服务同一个管理端接口的
source 切换：同一组过滤参数必须给出语义一致的结果集。本文件把「归一」
锁成被测试守卫的承诺——同一批种子记录写入两侧，同一组参数分别走
stats.list_requests 与 request_logs 后端的 list_requests，断言结果一致。

只断言外部可观察行为（过滤结果集 / 汇总数字 / 分页边界 / 行字段类型）：
不断言 SQL 字符串，不摸共享查询模块的私有符号。
接缝均为 spec 预约定：主缝 = 两侧 list_requests 公开面；日志侧按
ADR-0017 D3 直接实例化 SQLiteRequestLogBackend（tmp 库）。
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

import stats
from request_logs import SQLiteRequestLogBackend

pytestmark = pytest.mark.asyncio

ALL_MODELS = {"gpt-alpha", "gpt-beta", "gpt-gamma"}

# client_ip 故意含大小写差异的 IPv6 字面量：模糊搜归一为大小写不敏感后，
# 大写输入也要命中小写存储值（唯一用户可见行为变化，stats 侧原为大小写敏感）。
_IDENTITY_FIELDS = (
    "model",
    "channel_id",
    "channel_name",
    "api_key_id",
    "client_ip",
    "is_stream",
    "success",
    "request_source",
    "input_tokens",
    "output_tokens",
    "latency_ms",
    "lag_ms",
)


def _seed_records() -> list[dict]:
    """三条字段值各异的逻辑记录：两侧各写入一份，作为同一结果集的对照物。"""
    return [
        {
            "model": "gpt-alpha",
            "channel_id": "ch-alpha",
            "channel_name": "Alpha",
            "success": True,
            "is_stream": True,
            "api_key_id": "key-1",
            "client_ip": "2001:db8::1",
            "request_source": "client",
            "input_tokens": 10,
            "output_tokens": 5,
            "latency_ms": 100,
            "lag_ms": 10,
            "finish_reason": "stop",
            "error_msg": None,
        },
        {
            "model": "gpt-beta",
            "channel_id": "ch-beta",
            "channel_name": "Beta",
            "success": False,
            "is_stream": False,
            "api_key_id": "key-2",
            "client_ip": "198.51.100.22",
            "request_source": "admin_test",
            "input_tokens": 20,
            "output_tokens": 8,
            "latency_ms": 200,
            "lag_ms": None,
            "finish_reason": "error",
            "error_msg": "boom",
        },
        {
            "model": "gpt-gamma",
            "channel_id": "ch-gamma",
            "channel_name": "Gamma",
            "success": True,
            "is_stream": False,
            "api_key_id": "key-1",
            "client_ip": "2001:DB8::FF",
            "request_source": "group_probe",
            "input_tokens": 30,
            "output_tokens": 12,
            "latency_ms": 300,
            "lag_ms": 30,
            "finish_reason": "stop",
            "error_msg": None,
        },
    ]


async def _seed_stats(records: list[dict]) -> None:
    for record in records:
        stats.record_request(**record)
    await stats.drain_queue()


async def _seed_logs(backend: SQLiteRequestLogBackend, records: list[dict]) -> None:
    base = datetime.now(UTC)
    for index, record in enumerate(records):
        # 显式带时间戳写入（公开 write_record 入口），保证两侧记录时间同一量级
        await backend.write_record({**record, "timestamp": base + timedelta(milliseconds=index)})


@pytest_asyncio.fixture
async def stats_db(tmp_path):
    await stats.close_pool()
    await stats.init_db(str(tmp_path / "stats.db"))
    yield
    await stats.stop_stats_workers()
    await stats.close_pool()


@pytest_asyncio.fixture
async def logs_backend(tmp_path):
    backend = SQLiteRequestLogBackend(str(tmp_path / "request_logs.db"))
    await backend.init()
    yield backend
    await backend.close()


def _identity_rows(result: dict) -> list[tuple]:
    """把响应行投影到两侧共有字段的标识元组并排序——结果集比较用。"""
    return sorted(tuple(item[field] for field in _IDENTITY_FIELDS) for item in result["items"])


def _resolve_time_filters(filters: dict) -> dict:
    """把占位时间值解析为相对当前时刻的 datetime（含 aware 输入形态）。"""
    now = datetime.now(UTC)
    resolved = {}
    for key, value in filters.items():
        if value == "past":
            resolved[key] = (now - timedelta(hours=1)).replace(tzinfo=None)
        elif value == "future":
            resolved[key] = (now + timedelta(hours=1)).replace(tzinfo=None)
        elif value == "aware_past":
            resolved[key] = now - timedelta(hours=1)
        else:
            resolved[key] = value
    return resolved


@pytest.mark.parametrize(
    ("filters", "expected_models"),
    [
        ({}, ALL_MODELS),
        ({"model": "ALPHA"}, {"gpt-alpha"}),
        ({"model": "gpt-beta"}, {"gpt-beta"}),
        ({"channel": "Alpha"}, {"gpt-alpha"}),
        ({"channel": "ch-beta"}, {"gpt-beta"}),
        ({"start": "past"}, ALL_MODELS),
        ({"start": "future"}, set()),
        ({"start": "aware_past"}, ALL_MODELS),
        ({"end": "past"}, set()),
        ({"end": "future"}, ALL_MODELS),
        ({"success": True}, {"gpt-alpha", "gpt-gamma"}),
        ({"success": False}, {"gpt-beta"}),
        ({"api_key_id": "key-1"}, {"gpt-alpha", "gpt-gamma"}),
        ({"api_key_id": "key-2"}, {"gpt-beta"}),
        ({"client_ip": "DB8"}, {"gpt-alpha", "gpt-gamma"}),
        ({"client_ip": "198.51.100"}, {"gpt-beta"}),
        ({"is_stream": True}, {"gpt-alpha"}),
        ({"is_stream": False}, {"gpt-beta", "gpt-gamma"}),
        ({"request_source": "admin_test"}, {"gpt-beta"}),
        ({"request_source": ("group_probe", "admin_test")}, {"gpt-beta", "gpt-gamma"}),
        ({"request_source": None}, ALL_MODELS),
    ],
)
async def test_nine_conditions_yield_same_result_set_on_both_sides(stats_db, logs_backend, filters, expected_models):
    await _seed_stats(_seed_records())
    await _seed_logs(logs_backend, _seed_records())

    resolved = _resolve_time_filters(filters)
    stats_result = await stats.list_requests(**resolved)
    logs_result = await logs_backend.list_requests(**resolved)

    assert _identity_rows(stats_result) == _identity_rows(logs_result)
    assert stats_result["total"] == logs_result["total"] == len(expected_models)
    assert {item["model"] for item in stats_result["items"]} == expected_models


async def test_client_ip_fuzzy_search_is_case_insensitive_on_both_sides(stats_db, logs_backend):
    """唯一用户可见行为变化：stats 来源 client_ip 模糊搜由大小写敏感变不敏感。

    大写输入命中小写存储值；request_logs 来源行为保持不变。
    """
    records = _seed_records()
    await _seed_stats(records)
    await _seed_logs(logs_backend, records)

    stats_result = await stats.list_requests(client_ip="DB8::1")
    logs_result = await logs_backend.list_requests(client_ip="DB8::1")

    assert {item["model"] for item in stats_result["items"]} == {"gpt-alpha"}
    assert _identity_rows(stats_result) == _identity_rows(logs_result)


async def test_summary_aggregates_are_identical_on_both_sides(stats_db, logs_backend):
    """汇总条口径（success_count / token / 平均延迟 / 平均 lag）两侧一致且数字正确。"""
    await _seed_stats(_seed_records())
    await _seed_logs(logs_backend, _seed_records())

    stats_result = await stats.list_requests()
    logs_result = await logs_backend.list_requests()

    assert stats_result["summary"] == logs_result["summary"]
    assert stats_result["summary"] == {
        "total_requests": 3,
        "success_count": 2,
        "input_tokens": 60,
        "output_tokens": 25,
        "cache_read_input_tokens": 0,
        "avg_latency_ms": 200.0,
        "avg_lag_ms": 20.0,
    }


async def test_row_bool_fields_are_normalized_on_both_sides(stats_db, logs_backend):
    await _seed_stats(_seed_records())
    await _seed_logs(logs_backend, _seed_records())

    stats_item = (await stats.list_requests(is_stream=True))["items"][0]
    logs_item = (await logs_backend.list_requests(is_stream=True))["items"][0]

    for item in (stats_item, logs_item):
        assert item["success"] is True
        assert item["is_stream"] is True


async def test_pagination_normalization_is_identical_on_both_sides(stats_db, logs_backend):
    """page 下限 / page_size 上限 100 的归一两侧一致。"""
    await _seed_stats(_seed_records())
    await _seed_logs(logs_backend, _seed_records())

    stats_result = await stats.list_requests(page=0, page_size=1000)
    logs_result = await logs_backend.list_requests(page=0, page_size=1000)

    assert stats_result["page"] == logs_result["page"] == 1
    assert stats_result["page_size"] == logs_result["page_size"] == 100
    assert _identity_rows(stats_result) == _identity_rows(logs_result)


async def test_pagination_slices_identically_on_both_sides(stats_db, logs_backend):
    """排序（timestamp DESC, id DESC）下分页切片两侧一致。"""
    await _seed_stats(_seed_records())
    await _seed_logs(logs_backend, _seed_records())

    stats_page1 = await stats.list_requests(page=1, page_size=2)
    logs_page1 = await logs_backend.list_requests(page=1, page_size=2)
    stats_page2 = await stats.list_requests(page=2, page_size=2)
    logs_page2 = await logs_backend.list_requests(page=2, page_size=2)

    assert _identity_rows(stats_page1) == _identity_rows(logs_page1)
    assert _identity_rows(stats_page2) == _identity_rows(logs_page2)
    assert stats_page1["total"] == logs_page1["total"] == 3
    assert [item["model"] for item in stats_page1["items"]] == ["gpt-gamma", "gpt-beta"]
    assert [item["model"] for item in stats_page2["items"]] == ["gpt-alpha"]
