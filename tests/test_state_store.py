import asyncio
import json
import time

import pytest
from state_store import FileStore


@pytest.fixture
def tmp_data_dir(tmp_path):
    return str(tmp_path / "responses_session")


@pytest.fixture
def store(tmp_data_dir):
    return FileStore(data_dir=tmp_data_dir, max_entries=100, ttl_minutes=60)


def test_generate_response_id(store):
    rid = store.generate_response_id()
    assert rid.startswith("resp_")
    assert len(rid) == 29  # "resp_" + 24 hex chars


def test_put_and_get(store):
    rid = store.generate_response_id()
    conversation = {
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ],
        "reasoning_history": [],
        "tool_calls": [],
    }
    response = {
        "id": rid,
        "model": "gpt-4o",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hi there!"}],
            }
        ],
        "output_text": "Hi there!",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }

    asyncio.run(store.put(rid, conversation, response))

    result = asyncio.run(store.get_response(rid))
    assert result is not None
    assert result["id"] == rid
    assert result["model"] == "gpt-4o"

    conv = asyncio.run(store.get_conversation(rid))
    assert conv is not None
    assert len(conv["messages"]) == 3


def test_get_nonexistent(store):
    result = asyncio.run(store.get_response("resp_nonexistent"))
    assert result is None


def test_delete(store):
    rid = store.generate_response_id()
    conversation = {"messages": [], "reasoning_history": [], "tool_calls": []}
    response = {
        "id": rid,
        "model": "gpt-4o",
        "status": "completed",
        "output": [],
        "output_text": "",
        "usage": None,
    }

    asyncio.run(store.put(rid, conversation, response))
    assert asyncio.run(store.get_response(rid)) is not None

    deleted = asyncio.run(store.delete(rid))
    assert deleted is True
    assert asyncio.run(store.get_response(rid)) is None


def test_delete_nonexistent(store):
    deleted = asyncio.run(store.delete("resp_nonexistent"))
    assert deleted is False


def test_file_path_rejects_directory_traversal(store, tmp_path):
    """response_id 包含路径穿越时应抛出 ValueError"""
    traversal_ids = [
        "../../sensitive",
        "../sensitive",
        "sensitive/../../other",
        "sensitive/../other",
    ]
    for rid in traversal_ids:
        with pytest.raises(ValueError):
            store._file_path(rid)


def test_get_response_does_not_read_outside_data_dir(tmp_path):
    """路径穿越的 response_id 不应读取 data_dir 之外的文件"""
    data_dir = tmp_path / "responses_session"
    data_dir.mkdir()
    store = FileStore(data_dir=str(data_dir))

    sensitive_path = tmp_path / "sensitive.json"
    future = int(time.time()) + 3600
    sensitive_path.write_text(
        json.dumps(
            {
                "response_id": "../sensitive",
                "response": {"secret": "leaked"},
                "conversation": {},
                "created_at": int(time.time()),
                "expires_at": future,
                "last_access_at": int(time.time()),
            }
        ),
        encoding="utf-8",
    )

    result = asyncio.run(store.get_response("../sensitive"))
    assert result is None


def test_get_conversation_does_not_read_outside_data_dir(tmp_path):
    """路径穿越的 response_id 不应读取 data_dir 之外的对话记录"""
    data_dir = tmp_path / "responses_session"
    data_dir.mkdir()
    store = FileStore(data_dir=str(data_dir))

    sensitive_path = tmp_path / "conversation.json"
    future = int(time.time()) + 3600
    sensitive_path.write_text(
        json.dumps(
            {
                "response_id": "../conversation",
                "response": {},
                "conversation": {"messages": ["secret"]},
                "created_at": int(time.time()),
                "expires_at": future,
                "last_access_at": int(time.time()),
            }
        ),
        encoding="utf-8",
    )

    result = asyncio.run(store.get_conversation("../conversation"))
    assert result is None


def test_delete_does_not_remove_outside_data_dir(tmp_path):
    """路径穿越的 response_id 不应删除 data_dir 之外的文件"""
    data_dir = tmp_path / "responses_session"
    data_dir.mkdir()
    store = FileStore(data_dir=str(data_dir))

    outside_path = tmp_path / "outside.json"
    outside_path.write_text('{"data": "keep"}', encoding="utf-8")

    deleted = asyncio.run(store.delete("../outside"))
    assert deleted is False
    assert outside_path.exists()


def test_put_does_not_write_outside_data_dir(tmp_path):
    """路径穿越的 response_id 不应写入 data_dir 之外的文件"""
    data_dir = tmp_path / "responses_session"
    data_dir.mkdir()
    store = FileStore(data_dir=str(data_dir))

    with pytest.raises(ValueError):
        asyncio.run(store.put("../bad", {"messages": []}, {"id": "bad"}))

    assert not (tmp_path / "bad.json").exists()
