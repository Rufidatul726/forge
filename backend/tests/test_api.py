import asyncio
import logging
import re
from collections.abc import Mapping

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from forge.api.errors import ERROR_RESPONSES
from forge.api.main import create_app
from forge.config import ForgeSettings


def request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    raise_app_exceptions: bool = True,
) -> httpx.Response:
    async def send_request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, headers=headers)

    return asyncio.run(send_request())


@pytest.fixture
def settings() -> ForgeSettings:
    return ForgeSettings(_env_file=None)


@pytest.fixture
def app(settings: ForgeSettings) -> FastAPI:
    return create_app(settings)


def test_health_endpoint_has_a_correlatable_request_id(app: FastAPI, caplog):
    with caplog.at_level(logging.INFO, logger="forge.api"):
        response = request(app, "GET", "/api/v1/health")

    request_id = response.headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    assert response.json() == {"status": "ok", "service": "forge-api"}
    assert f"request_id={request_id}" in caplog.text


def test_provider_endpoint_uses_injected_settings():
    settings = ForgeSettings(
        _env_file=None,
        gemini_api_key="test-key",
        openrouter_api_key=None,
    )
    response = request(create_app(settings), "GET", "/api/v1/providers")

    assert response.status_code == 200
    payload = response.json()
    providers = {provider["name"]: provider for provider in payload["providers"]}
    assert providers["gemini"]["configured"] is True
    assert providers["openrouter"]["configured"] is False


def test_unknown_route_returns_stable_error_contract(app: FastAPI):
    response = request(app, "GET", "/api/v1/unknown")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "Resource not found."},
        "request_id": response.headers["X-Request-ID"],
    }


def test_validation_error_does_not_echo_invalid_input(settings: ForgeSettings):
    app = create_app(settings)

    @app.get("/test/{item_id}", responses={422: ERROR_RESPONSES[422]})
    def read_item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    response = request(app, "GET", "/test/private-value")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert response.json()["details"][0]["location"] == "path.item_id"
    assert "private-value" not in response.text


def test_unexpected_error_is_sanitized_and_logged_with_request_id(
    settings: ForgeSettings,
    caplog,
):
    app = create_app(settings)

    @app.get("/test-error")
    def raise_error() -> None:
        raise RuntimeError("sensitive exception detail")

    with caplog.at_level(logging.ERROR, logger="forge.api"):
        response = request(
            app,
            "GET",
            "/test-error",
            raise_app_exceptions=False,
        )

    request_id = response.headers["X-Request-ID"]
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "An unexpected error occurred."},
        "request_id": request_id,
    }
    assert f"request_id={request_id}" in caplog.text
    assert "sensitive exception detail" not in caplog.text


def test_cors_allows_configured_frontend_origin(app: FastAPI):
    preflight = request(
        app,
        "OPTIONS",
        "/api/v1/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    response = request(
        app,
        "GET",
        "/api/v1/health",
        headers={"Origin": "http://localhost:5173"},
    )

    assert preflight.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert "Access-Control-Allow-Credentials" not in preflight.headers
    assert "GET" in preflight.headers["Access-Control-Allow-Methods"]
    assert response.headers["Access-Control-Expose-Headers"] == "X-Request-ID"


def test_production_settings_require_explicit_secure_cors_origins():
    with pytest.raises(ValidationError, match="Production CORS origins"):
        ForgeSettings(_env_file=None, environment="production")

    settings = ForgeSettings(
        _env_file=None,
        environment="production",
        cors_origins="https://forge.example.com",
    )
    assert settings.allowed_origins == ["https://forge.example.com"]


@pytest.mark.parametrize(
    "cors_origins",
    ["", "*", "https://forge.example.com/path", "ftp://forge.example.com"],
)
def test_settings_reject_invalid_cors_origins(cors_origins: str):
    with pytest.raises(ValidationError):
        ForgeSettings(_env_file=None, cors_origins=cors_origins)


def test_openapi_documents_error_contracts(app: FastAPI):
    schema = request(app, "GET", "/openapi.json").json()
    responses = schema["paths"]["/api/v1/providers"]["get"]["responses"]

    assert responses["404"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorResponse"
    )
    assert responses["422"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorResponse"
    )
    assert responses["500"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorResponse"
    )
