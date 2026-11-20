from fastapi.testclient import TestClient

from forge.api.main import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "forge-api"}


def test_provider_endpoint_reports_configuration(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    response = client.get("/api/v1/providers")
    assert response.status_code == 200
    payload = response.json()
    providers = {provider["name"]: provider for provider in payload["providers"]}
    assert providers["gemini"]["configured"] is True
    assert providers["openrouter"]["configured"] is False
