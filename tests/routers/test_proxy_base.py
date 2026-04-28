from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_invalid_json_request_returns_400():
    """测试无效JSON请求返回400错误"""
    response = client.post(
        "/v1/chat/completions",
        content=b"not valid json {",
        headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert "Invalid JSON" in response.text or "invalid" in response.text.lower()


def test_missing_model_returns_error():
    """测试缺失model字段返回错误"""
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    # 应该返回错误（没有渠道支持空模型）
    assert response.status_code in (400, 500)
