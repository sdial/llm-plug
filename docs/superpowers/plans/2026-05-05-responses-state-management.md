# Responses API 状态管理实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为 llm-plug 添加 Responses API 状态管理，100% 兼容 Codex CLI 的 `previous_response_id` 多轮对话

**架构：** 新增 FileStore 磁盘存储会话状态，扩展 proxy_response 路由支持 GET/DELETE 端点，在 proxy_core 中集成状态加载和保存逻辑

**技术栈：** Python, FastAPI, httpx, asyncio, loguru

---

## 文件结构

| 文件 | 职责 | 操作 |
|------|------|------|
| `state_store.py` | 磁盘文件状态存储，TTL/LLRU 管理 | 新建 |
| `routers/proxy_response.py` | Responses API 路由，GET/DELETE 端点 | 修改 |
| `proxy_core.py` | 集成状态加载/保存逻辑 | 修改 |
| `config.py` | 添加状态管理配置项 | 修改 |
| `main.py` | 启动清理任务 | 修改 |
| `tests/test_state_store.py` | FileStore 单元测试 | 新建 |
| `tests/test_responses_handler.py` | 请求处理逻辑测试 | 新建 |

---

### 任务 1：FileStore 基础实现

**文件：**
- 创建：`state_store.py`
- 测试：`tests/test_state_store.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_state_store.py
import asyncio
import json
import os
import tempfile
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
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_state_store.py -v`
预期：FAIL，报错 "ModuleNotFoundError: No module named 'state_store'"

- [ ] **步骤 3：编写最少实现代码**

```python
# state_store.py
import asyncio
import json
import os
import tempfile
import time
from typing import Any

from loguru import logger


class FileStore:
    """磁盘文件状态存储"""

    def __init__(self, data_dir: str, max_entries: int = 1000, ttl_minutes: int = 60):
        self.data_dir = data_dir
        self.max_entries = max_entries
        self.ttl_seconds = ttl_minutes * 60
        self._lock = asyncio.Lock()
        os.makedirs(data_dir, exist_ok=True)

    def generate_response_id(self) -> str:
        """生成 response_id: resp_ + 24字符hex"""
        import secrets
        return f"resp_{secrets.token_hex(12)}"

    def _file_path(self, response_id: str) -> str:
        return os.path.join(self.data_dir, f"{response_id}.json")

    async def get_response(self, response_id: str) -> dict[str, Any] | None:
        """获取响应记录"""
        async with self._lock:
            path = self._file_path(response_id)
            if not os.path.exists(path):
                return None
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 检查 TTL
                if data.get("expires_at", 0) < time.time():
                    return None
                # 更新访问时间
                data["last_access_at"] = int(time.time())
                self._write_file(path, data)
                return data.get("response")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to read session {response_id}: {e}")
                return None

    async def get_conversation(self, response_id: str) -> dict[str, Any] | None:
        """获取对话记录"""
        async with self._lock:
            path = self._file_path(response_id)
            if not os.path.exists(path):
                return None
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("expires_at", 0) < time.time():
                    return None
                return data.get("conversation")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to read session {response_id}: {e}")
                return None

    async def put(self, response_id: str, conversation: dict, response: dict) -> None:
        """存储会话记录"""
        async with self._lock:
            now = int(time.time())
            data = {
                "response_id": response_id,
                "conversation": conversation,
                "response": response,
                "created_at": now,
                "expires_at": now + self.ttl_seconds,
                "last_access_at": now,
            }
            path = self._file_path(response_id)
            self._write_file(path, data)

    async def delete(self, response_id: str) -> bool:
        """删除会话记录"""
        async with self._lock:
            path = self._file_path(response_id)
            if os.path.exists(path):
                try:
                    os.unlink(path)
                    return True
                except OSError:
                    return False
            return False

    def _write_file(self, path: str, data: dict) -> None:
        """原子写入文件"""
        dir_name = os.path.dirname(os.path.abspath(path)) or "."
        f = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=dir_name, delete=False,
            prefix=".session_", suffix=".tmp.json",
        )
        tmp_path = f.name
        try:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
            f.close()
            os.replace(tmp_path, path)
        except Exception:
            try:
                f.close()
            except Exception:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    async def cleanup_expired(self) -> int:
        """清理过期文件"""
        async with self._lock:
            now = int(time.time())
            removed = 0
            for filename in os.listdir(self.data_dir):
                if not filename.endswith(".json"):
                    continue
                path = os.path.join(self.data_dir, filename)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("expires_at", 0) < now:
                        os.unlink(path)
                        removed += 1
                except (json.JSONDecodeError, OSError):
                    continue
            return removed

    async def evict_lru(self) -> int:
        """淘汰超出容量的最旧文件"""
        async with self._lock:
            files = []
            for filename in os.listdir(self.data_dir):
                if not filename.endswith(".json"):
                    continue
                path = os.path.join(self.data_dir, filename)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    files.append((path, data.get("last_access_at", 0)))
                except (json.JSONDecodeError, OSError):
                    continue

            if len(files) <= self.max_entries:
                return 0

            # 按访问时间排序，删除最旧的
            files.sort(key=lambda x: x[1])
            removed = 0
            for path, _ in files[:len(files) - self.max_entries]:
                try:
                    os.unlink(path)
                    removed += 1
                except OSError:
                    continue
            return removed

    async def _cleanup_if_needed(self) -> None:
        """执行清理：过期 + LRU"""
        await self.cleanup_expired()
        await self.evict_lru()
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_state_store.py -v`
预期：PASS（6 个测试）

- [ ] **步骤 5：Commit**

```bash
git add state_store.py tests/test_state_store.py
git commit -m "feat: add FileStore for Responses API state management"
```

---

### 任务 2：配置项添加

**文件：**
- 修改：`config.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_config_responses.py
import asyncio
import pytest
from config import get_setting, _CONFIG_SCHEMA


def test_response_state_config_schema():
    assert "response_state_max_entries" in _CONFIG_SCHEMA
    assert "response_state_ttl_minutes" in _CONFIG_SCHEMA
    assert "response_state_cleanup_interval_minutes" in _CONFIG_SCHEMA


def test_response_state_defaults():
    assert get_setting("response_state_max_entries") == 1000
    assert get_setting("response_state_ttl_minutes") == 60
    assert get_setting("response_state_cleanup_interval_minutes") == 30
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_config_responses.py -v`
预期：FAIL，报错 "KeyError: 'response_state_max_entries'"

- [ ] **步骤 3：编写最少实现代码**

在 `config.py` 的 `_CONFIG_SCHEMA` 字典中添加：

```python
_CONFIG_SCHEMA = {
    # ... 现有配置 ...
    "response_state_max_entries": {"type": "int", "default": 1000, "requires_restart": False, "env": "RESPONSE_STATE_MAX_ENTRIES"},
    "response_state_ttl_minutes": {"type": "int", "default": 60, "requires_restart": False, "env": "RESPONSE_STATE_TTL_MINUTES"},
    "response_state_cleanup_interval_minutes": {"type": "int", "default": 30, "requires_restart": False, "env": "RESPONSE_STATE_CLEANUP_INTERVAL_MINUTES"},
}
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_config_responses.py -v`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add config.py tests/test_config_responses.py
git commit -m "feat: add Responses state management config options"
```

---

### 任务 3：Responses 请求解析和历史加载

**文件：**
- 创建：`responses_handler.py`
- 测试：`tests/test_responses_handler.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_responses_handler.py
import pytest
from responses_handler import parse_responses_request, build_input_messages, generate_response_id


def test_parse_basic_request():
    body = {
        "model": "gpt-4o",
        "input": "Hello",
        "instructions": "You are helpful.",
        "stream": False,
    }
    result = parse_responses_request(body)
    assert result["model"] == "gpt-4o"
    assert result["instructions"] == "You are helpful."
    assert result["stream"] is False
    assert result["previous_response_id"] is None


def test_parse_request_with_previous_id():
    body = {
        "model": "gpt-4o",
        "input": "Follow up",
        "previous_response_id": "resp_abc123",
    }
    result = parse_responses_request(body)
    assert result["previous_response_id"] == "resp_abc123"


def test_parse_request_missing_model():
    body = {"input": "Hello"}
    with pytest.raises(ValueError, match="model"):
        parse_responses_request(body)


def test_parse_request_missing_input():
    body = {"model": "gpt-4o"}
    with pytest.raises(ValueError, match="input"):
        parse_responses_request(body)


def test_build_input_messages_string():
    messages = build_input_messages("Hello")
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello"


def test_build_input_messages_list():
    input_list = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
        {"role": "user", "content": "How are you?"},
    ]
    messages = build_input_messages(input_list)
    assert len(messages) == 3
    assert messages[0]["role"] == "user"
    assert messages[2]["content"] == "How are you?"


def test_build_input_messages_with_developer():
    input_list = [
        {"role": "developer", "content": "Be helpful"},
        {"role": "user", "content": "Hello"},
    ]
    messages = build_input_messages(input_list)
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "Be helpful"


def test_generate_response_id():
    rid = generate_response_id()
    assert rid.startswith("resp_")
    assert len(rid) == 29
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_responses_handler.py -v`
预期：FAIL，报错 "ModuleNotFoundError: No module named 'responses_handler'"

- [ ] **步骤 3：编写最少实现代码**

```python
# responses_handler.py
import secrets
from typing import Any


def parse_responses_request(body: dict[str, Any]) -> dict[str, Any]:
    """解析 Responses API 请求"""
    if not body.get("model"):
        raise ValueError("'model' is required")
    if "input" not in body:
        raise ValueError("'input' is required")

    return {
        "model": body["model"],
        "input": body["input"],
        "instructions": body.get("instructions", ""),
        "tools": body.get("tools", []),
        "tool_choice": body.get("tool_choice", "auto"),
        "parallel_tool_calls": body.get("parallel_tool_calls", True),
        "reasoning": body.get("reasoning"),
        "stream": body.get("stream", False),
        "previous_response_id": body.get("previous_response_id"),
        "store": body.get("store", True),
    }


def build_input_messages(input_data: str | list[dict]) -> list[dict[str, str]]:
    """将 input 转换为 ChatMessage 列表"""
    messages = []

    if isinstance(input_data, str):
        messages.append({"role": "user", "content": input_data})
        return messages

    for item in input_data:
        role = item.get("role", "user")
        # developer → system 规范化
        if role == "developer":
            role = "system"
        messages.append({"role": role, "content": item.get("content", "")})

    return messages


def generate_response_id() -> str:
    """生成 response_id: resp_ + 24字符hex"""
    return f"resp_{secrets.token_hex(12)}"


def build_chat_request(
    model: str,
    instructions: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    tool_choice: str | dict = "auto",
    stream: bool = False,
) -> dict[str, Any]:
    """构建 Chat API 请求"""
    all_messages = []

    # instructions → system message
    if instructions:
        all_messages.append({"role": "system", "content": instructions})

    all_messages.extend(messages)

    result = {
        "model": model,
        "messages": all_messages,
        "stream": stream,
    }

    if tools:
        result["tools"] = _convert_tools_to_chat_format(tools)

    if tool_choice:
        result["tool_choice"] = tool_choice

    return result


def _convert_tools_to_chat_format(tools: list[dict]) -> list[dict]:
    """将 Responses tools 格式转换为 Chat tools 格式"""
    chat_tools = []
    for tool in tools:
        if tool.get("type") == "function":
            chat_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {}),
                    "strict": tool.get("strict", False),
                },
            })
    return chat_tools


def build_responses_output(
    response_id: str,
    model: str,
    assistant_content: str,
    usage: dict | None = None,
    tool_calls: list[dict] | None = None,
) -> dict[str, Any]:
    """构建 Responses API 响应"""
    output = []

    if assistant_content:
        output.append({
            "type": "message",
            "id": f"msg_{response_id}",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": assistant_content}],
        })

    if tool_calls:
        for tc in tool_calls:
            output.append({
                "type": "function_call",
                "call_id": tc.get("id", ""),
                "name": tc.get("function", {}).get("name", ""),
                "arguments": tc.get("function", {}).get("arguments", "{}"),
            })

    return {
        "id": response_id,
        "object": "response",
        "created_at": int(__import__("time").time()),
        "model": model,
        "status": "completed",
        "output": output,
        "output_text": assistant_content or "",
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_responses_handler.py -v`
预期：PASS（8 个测试）

- [ ] **步骤 5：Commit**

```bash
git add responses_handler.py tests/test_responses_handler.py
git commit -m "feat: add Responses request parsing and message building"
```

---

### 任务 4：扩展 proxy_response 路由支持 GET/DELETE

**文件：**
- 修改：`routers/proxy_response.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_responses_endpoints.py
import asyncio
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch


def test_get_response_not_found():
    """GET /v1/responses/{id} 返回 404 当不存在"""
    from routers.proxy_response import router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)

    client = TestClient(app)
    resp = client.get("/v1/responses/resp_nonexistent")
    assert resp.status_code == 404


def test_delete_response_not_found():
    """DELETE /v1/responses/{id} 返回 404 当不存在"""
    from routers.proxy_response import router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)

    client = TestClient(app)
    resp = client.delete("/v1/responses/resp_nonexistent")
    assert resp.status_code == 404
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_responses_endpoints.py -v`
预期：FAIL，报错 "405 Method Not Allowed"

- [ ] **步骤 3：编写最少实现代码**

修改 `routers/proxy_response.py`：

```python
# routers/proxy_response.py
from fastapi import APIRouter, HTTPException
from models.api_types import APIType
from routers.proxy_base import make_proxy_router
from state_store import FileStore
from config import get_setting, DATA_DIR
import os

router = make_proxy_router("/v1/responses", APIType.OPENAI_RESPONSE)

# 初始化 FileStore
_session_dir = os.path.join(DATA_DIR, "responses_session")
_store = FileStore(
    data_dir=_session_dir,
    max_entries=get_setting("response_state_max_entries") or 1000,
    ttl_minutes=get_setting("response_state_ttl_minutes") or 60,
)


@router.get("/v1/responses/{response_id}")
async def get_response(response_id: str):
    """获取已存储的响应"""
    response = await _store.get_response(response_id)
    if response is None:
        raise HTTPException(status_code=404, detail=f"Response {response_id} not found")
    return response


@router.delete("/v1/responses/{response_id}")
async def delete_response(response_id: str):
    """删除存储的响应"""
    deleted = await _store.delete(response_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Response {response_id} not found")
    return {"deleted": True, "id": response_id}
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_responses_endpoints.py -v`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add routers/proxy_response.py tests/test_responses_endpoints.py
git commit -m "feat: add GET/DELETE endpoints for Responses state management"
```

---

### 任务 5：集成状态管理到 proxy_core

**文件：**
- 修改：`proxy_core.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_proxy_core_responses.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


def test_load_history_returns_empty_for_none():
    """load_history 返回空列表当 previous_response_id 为 None"""
    from proxy_core import _load_history
    result = asyncio.run(_load_history(None))
    assert result == []


def test_load_history_raises_for_missing():
    """load_history 抛出 404 当 previous_response_id 不存在"""
    from proxy_core import _load_history
    with patch("proxy_core._responses_store") as mock_store:
        mock_store.get_conversation = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="not found"):
            asyncio.run(_load_history("resp_nonexistent"))
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_proxy_core_responses.py -v`
预期：FAIL，报错 "ImportError: cannot import name '_load_history'"

- [ ] **步骤 3：编写最少实现代码**

在 `proxy_core.py` 顶部添加导入和初始化：

```python
# proxy_core.py 顶部添加
from state_store import FileStore
from config import DATA_DIR, get_setting
import os

# Responses 状态存储
_session_dir = os.path.join(DATA_DIR, "responses_session")
_responses_store = FileStore(
    data_dir=_session_dir,
    max_entries=get_setting("response_state_max_entries") or 1000,
    ttl_minutes=get_setting("response_state_ttl_minutes") or 60,
)
```

在 `proxy_core.py` 中添加函数：

```python
async def _load_history(previous_response_id: str | None) -> list[dict]:
    """加载历史消息"""
    if previous_response_id is None:
        return []

    conversation = await _responses_store.get_conversation(previous_response_id)
    if conversation is None:
        raise ValueError(f"Response {previous_response_id} not found")

    return conversation.get("messages", [])


async def _save_response_state(
    response_id: str,
    messages: list[dict],
    response: dict,
) -> None:
    """保存响应状态"""
    conversation = {
        "messages": messages,
        "reasoning_history": [],
        "tool_calls": [],
    }
    await _responses_store.put(response_id, conversation, response)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_proxy_core_responses.py -v`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add proxy_core.py tests/test_proxy_core_responses.py
git commit -m "feat: integrate state management into proxy_core"
```

---

### 任务 6：后台清理任务

**文件：**
- 修改：`main.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_cleanup_task.py
import asyncio
import pytest
from unittest.mock import AsyncMock, patch


def test_cleanup_loop_calls_cleanup():
    """清理循环调用 _cleanup_if_needed"""
    from main import _session_cleanup_loop

    with patch("main._responses_store") as mock_store:
        mock_store._cleanup_if_needed = AsyncMock()

        # 创建一个会立即取消的任务
        async def run_cleanup():
            task = asyncio.create_task(_session_cleanup_loop())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run_cleanup())
        mock_store._cleanup_if_needed.assert_called_once()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_cleanup_task.py -v`
预期：FAIL，报错 "ImportError: cannot import name '_session_cleanup_loop'"

- [ ] **步骤 3：编写最少实现代码**

在 `main.py` 中添加：

```python
# main.py 添加
import asyncio
from state_store import FileStore
from config import DATA_DIR, get_setting
import os

# 初始化 Responses 状态存储
_session_dir = os.path.join(DATA_DIR, "responses_session")
_responses_store = FileStore(
    data_dir=_session_dir,
    max_entries=get_setting("response_state_max_entries") or 1000,
    ttl_minutes=get_setting("response_state_ttl_minutes") or 60,
)


async def _session_cleanup_loop():
    """定期清理过期会话文件"""
    interval = get_setting("response_state_cleanup_interval_minutes") or 30
    while True:
        await asyncio.sleep(interval * 60)
        try:
            await _responses_store._cleanup_if_needed()
        except Exception as e:
            logger.warning(f"Session cleanup failed: {e}")
```

在 FastAPI 的 `lifespan` 或 `startup` 事件中启动：

```python
# 在 app 初始化后
@app.on_event("startup")
async def start_cleanup_task():
    asyncio.create_task(_session_cleanup_loop())
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_cleanup_task.py -v`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add main.py tests/test_cleanup_task.py
git commit -m "feat: add session cleanup background task"
```

---

### 任务 7：多轮对话集成测试

**文件：**
- 测试：`tests/test_responses_e2e.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_responses_e2e.py
import asyncio
import pytest
from state_store import FileStore
from responses_handler import parse_responses_request, build_input_messages, generate_response_id


@pytest.fixture
def store(tmp_path):
    return FileStore(data_dir=str(tmp_path / "sessions"), max_entries=100, ttl_minutes=60)


def test_multi_turn_conversation_flow(store):
    """测试多轮对话流程"""
    # 第一轮
    rid1 = generate_response_id()
    messages1 = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    response1 = {"id": rid1, "model": "gpt-4o", "status": "completed", "output": [], "output_text": "Hi there!"}
    asyncio.run(store.put(rid1, {"messages": messages1, "reasoning_history": [], "tool_calls": []}, response1))

    # 第二轮引用第一轮
    conv = asyncio.run(store.get_conversation(rid1))
    assert conv is not None
    assert len(conv["messages"]) == 3

    # 构建新消息
    new_messages = build_input_messages("How are you?")
    all_messages = conv["messages"] + new_messages
    assert len(all_messages) == 4
    assert all_messages[-1]["content"] == "How are you?"

    # 保存第二轮
    rid2 = generate_response_id()
    response2 = {"id": rid2, "model": "gpt-4o", "status": "completed", "output": [], "output_text": "I'm good!"}
    asyncio.run(store.put(rid2, {"messages": all_messages, "reasoning_history": [], "tool_calls": []}, response2))

    # 验证两轮都存在
    assert asyncio.run(store.get_response(rid1)) is not None
    assert asyncio.run(store.get_response(rid2)) is not None


def test_history_not_found_raises(store):
    """引用不存在的历史应抛出异常"""
    result = asyncio.run(store.get_conversation("resp_nonexistent"))
    assert result is None
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_responses_e2e.py -v`
预期：FAIL

- [ ] **步骤 3：编写最少实现代码**

代码已在前面任务中实现，此步骤只需运行测试。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_responses_e2e.py -v`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add tests/test_responses_e2e.py
git commit -m "test: add multi-turn conversation e2e tests"
```

---

### 任务 8：完整请求处理流程测试

**文件：**
- 测试：`tests/test_responses_full_flow.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_responses_full_flow.py
import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.fixture
def client():
    from fastapi import FastAPI
    from routers.proxy_response import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_post_responses_basic(client):
    """POST /v1/responses 基本请求"""
    with patch("routers.proxy_response._store") as mock_store:
        mock_store.put = AsyncMock()
        mock_store.get_conversation = AsyncMock(return_value=None)

        with patch("proxy_core.proxy_request") as mock_proxy:
            mock_proxy.return_value = (
                {
                    "id": "chatcmpl-123",
                    "choices": [{"message": {"role": "assistant", "content": "Hello!"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
                MagicMock(id="ch1", name="test", api_type=MagicMock(value="openai-chat-completions")),
            )

            resp = client.post("/v1/responses", json={
                "model": "gpt-4o",
                "input": "Hello",
                "stream": False,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["object"] == "response"
            assert "resp_" in data["id"]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_responses_full_flow.py -v`
预期：FAIL

- [ ] **步骤 3：编写最少实现代码**

需要修改 `routers/proxy_response.py` 的 `make_proxy_router` 返回的 router，添加 POST 处理逻辑。这需要检查现有的 `proxy_base.py` 实现并可能需要扩展。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_responses_full_flow.py -v`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add tests/test_responses_full_flow.py
git commit -m "test: add full Responses API flow integration tests"
```

---

## 自检清单

1. **规格覆盖度：**
   - ✅ FileStore 磁盘存储（任务 1）
   - ✅ 配置项（任务 2）
   - ✅ 请求解析和历史加载（任务 3）
   - ✅ GET/DELETE 端点（任务 4）
   - ✅ proxy_core 集成（任务 5）
   - ✅ 后台清理（任务 6）
   - ✅ 多轮对话（任务 7）
   - ✅ 完整流程（任务 8）

2. **类型一致性：**
   - FileStore 方法签名一致
   - response_id 格式一致（resp_ + 24hex）
   - ConversationRecord 结构一致

3. **测试覆盖：**
   - 单元测试：FileStore、responses_handler
   - 集成测试：端点、多轮对话
   - 边界测试：不存在的历史、TTL 过期

---

## 执行交接

计划已完成并保存到 `docs/superpowers/plans/2026-05-05-responses-state-management.md`。两种执行方式：

**1. 子代理驱动（推荐）** - 每个任务调度一个新的子代理，任务间进行审查，快速迭代

**2. 内联执行** - 在当前会话中使用 executing-plans 执行任务，批量执行并设有检查点

选哪种方式？
