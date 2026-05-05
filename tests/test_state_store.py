import asyncio

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
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hi there!"}]}],
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
    response = {"id": rid, "model": "gpt-4o", "status": "completed", "output": [], "output_text": "", "usage": None}

    asyncio.run(store.put(rid, conversation, response))
    assert asyncio.run(store.get_response(rid)) is not None

    deleted = asyncio.run(store.delete(rid))
    assert deleted is True
    assert asyncio.run(store.get_response(rid)) is None


def test_delete_nonexistent(store):
    deleted = asyncio.run(store.delete("resp_nonexistent"))
    assert deleted is False
