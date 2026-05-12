from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_get_response_not_found():
    """GET /v1/responses/{id} 返回 404 当不存在"""
    from routers.proxy_response import router

    app = FastAPI()
    app.include_router(router)

    client = TestClient(app)
    resp = client.get("/v1/responses/resp_nonexistent")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_delete_response_not_found():
    """DELETE /v1/responses/{id} 返回 404 当不存在"""
    from routers.proxy_response import router

    app = FastAPI()
    app.include_router(router)

    client = TestClient(app)
    resp = client.delete("/v1/responses/resp_nonexistent")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_get_response_endpoint_exists():
    """GET /v1/responses/{id} 端点存在且有正确的路径参数"""
    from routers.proxy_response import router

    # 验证 GET 路由存在
    get_routes = [r for r in router.routes if "GET" in getattr(r, "methods", set())]
    assert len(get_routes) > 0, "GET route should exist"
    assert any("response_id" in str(r.path) for r in get_routes), (
        "Route should have response_id parameter"
    )


def test_delete_response_endpoint_exists():
    """DELETE /v1/responses/{id} 端点存在且有正确的路径参数"""
    from routers.proxy_response import router

    # 验证 DELETE 路由存在
    delete_routes = [
        r for r in router.routes if "DELETE" in getattr(r, "methods", set())
    ]
    assert len(delete_routes) > 0, "DELETE route should exist"
    assert any("response_id" in str(r.path) for r in delete_routes), (
        "Route should have response_id parameter"
    )
