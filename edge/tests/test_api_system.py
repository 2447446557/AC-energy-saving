"""系统接口测试"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_check():
    """测试健康检查接口"""
    from app.main import create_app

    app = create_app()
    client = TestClient(app)

    response = client.get("/api/v1/system/health")
    assert response.status_code == 200

    data = response.json()
    assert data["code"] == 0
    assert data["data"]["status"] == "ok"
    assert "version" in data["data"]
    assert "uptime" in data["data"]


def test_version():
    """测试版本接口"""
    from app.main import create_app

    app = create_app()
    client = TestClient(app)

    response = client.get("/api/v1/system/version")
    assert response.status_code == 200

    data = response.json()
    assert data["code"] == 0
    assert "version" in data["data"]
