import asyncio
import pytest
from state_store import FileStore
from routers.proxy_response import _input_to_items


@pytest.fixture
def store(tmp_path):
    return FileStore(
        data_dir=str(tmp_path / "sessions"), max_entries=100, ttl_minutes=60
    )


def test_multi_turn_conversation_flow(store):
    """测试多轮对话流程"""
    # 第一轮
    rid1 = store.generate_response_id()
    messages1 = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    response1 = {
        "id": rid1,
        "model": "gpt-4o",
        "status": "completed",
        "output": [],
        "output_text": "Hi there!",
    }
    asyncio.run(
        store.put(
            rid1,
            {"messages": messages1, "reasoning_history": [], "tool_calls": []},
            response1,
        )
    )

    # 第二轮引用第一轮
    conv = asyncio.run(store.get_conversation(rid1))
    assert conv is not None
    assert len(conv["messages"]) == 3

    # 构建新消息
    new_messages = _input_to_items("How are you?")
    all_messages = conv["messages"] + new_messages
    assert len(all_messages) == 4
    assert all_messages[-1]["content"] == "How are you?"

    # 保存第二轮
    rid2 = store.generate_response_id()
    response2 = {
        "id": rid2,
        "model": "gpt-4o",
        "status": "completed",
        "output": [],
        "output_text": "I'm good!",
    }
    asyncio.run(
        store.put(
            rid2,
            {"messages": all_messages, "reasoning_history": [], "tool_calls": []},
            response2,
        )
    )

    # 验证两轮都存在
    assert asyncio.run(store.get_response(rid1)) is not None
    assert asyncio.run(store.get_response(rid2)) is not None


def test_history_not_found_raises(store):
    """引用不存在的历史应抛出异常"""
    result = asyncio.run(store.get_conversation("resp_nonexistent"))
    assert result is None
