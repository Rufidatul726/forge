import asyncio
import json
import logging
import re
from collections.abc import Mapping

import httpx
import pytest
from fastapi import FastAPI
from forge.api.errors import ERROR_RESPONSES
from forge.api.main import create_app
from forge.config import ForgeSettings
from pydantic import ValidationError


def request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Mapping[str, object] | None = None,
    raise_app_exceptions: bool = True,
) -> httpx.Response:
    async def send_request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(
                method,
                path,
                headers=headers,
                json=json_body,
            )

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


def test_readiness_endpoint_requires_workspace_and_api_key(tmp_path):
    api_key = "a" * 40
    configured = request(
        create_app(
            ForgeSettings(
                _env_file=None,
                forge_api_key=api_key,
                agent_workspace_root=tmp_path,
            )
        ),
        "GET",
        "/api/v1/ready",
    )
    missing = request(
        create_app(ForgeSettings(_env_file=None)),
        "GET",
        "/api/v1/ready",
    )

    assert configured.status_code == 200
    assert configured.json() == {
        "status": "ready",
        "workspace": True,
        "api_key": True,
    }
    assert missing.status_code == 503


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


def test_generation_endpoint_requires_configured_and_valid_api_key():
    payload = {
        "model": "qwen2.5-coder:7b",
        "messages": [{"role": "user", "content": "Explain this code"}],
    }
    unconfigured = request(
        create_app(ForgeSettings(_env_file=None)),
        "POST",
        "/api/v1/generate",
        json_body=payload,
    )
    configured_app = create_app(
        ForgeSettings(_env_file=None, forge_api_key="a" * 40)
    )
    unauthorized = request(
        configured_app,
        "POST",
        "/api/v1/generate",
        headers={"Authorization": f"Bearer {'b' * 40}"},
        json_body=payload,
    )

    assert unconfigured.status_code == 503
    assert unconfigured.json()["error"]["code"] == "service_unavailable"
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "unauthorized"


def test_expensive_routes_are_rate_limited_per_direct_client():
    app = create_app(
        ForgeSettings(
            _env_file=None,
            forge_api_key="a" * 40,
            rate_limit_requests=2,
            rate_limit_window_seconds=60,
        )
    )
    payload = {
        "model": "qwen2.5-coder:7b",
        "messages": [{"role": "user", "content": "Explain this code"}],
    }

    headers = {"Authorization": f"Bearer {'a' * 40}"}
    first = request(app, "POST", "/api/v1/generate", headers=headers, json_body=payload)
    second = request(app, "POST", "/api/v1/generate", headers=headers, json_body=payload)
    limited = request(app, "POST", "/api/v1/generate", headers=headers, json_body=payload)

    assert first.status_code == 503
    assert second.status_code == 503
    assert limited.status_code == 429
    assert limited.json()["error"] == {
        "code": "rate_limited",
        "message": "Too many requests. Please try again later.",
    }
    assert limited.json()["request_id"] == limited.headers["X-Request-ID"]
    assert int(limited.headers["Retry-After"]) >= 1


def test_generation_endpoint_calls_selected_provider_and_returns_result(monkeypatch):
    from forge.providers.models import GenerationResult

    class FakeProvider:
        async def generate(self, payload):
            assert payload.model == "qwen2.5-coder:7b"
            assert payload.messages[0].content == "Explain this code"
            return GenerationResult(
                model=payload.model,
                content="It maps each item.",
                done=True,
            )

    provider_names = []

    def fake_get_provider(name, settings):
        provider_names.append(name)
        return FakeProvider()

    monkeypatch.setattr("forge.api.routes.generation.get_provider", fake_get_provider)
    api_key = "a" * 40
    response = request(
        create_app(ForgeSettings(_env_file=None, forge_api_key=api_key)),
        "POST",
        "/api/v1/generate",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body={
            "model": "qwen2.5-coder:7b",
            "messages": [{"role": "user", "content": "Explain this code"}],
            "max_tokens": 128,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "model": "qwen2.5-coder:7b",
        "content": "It maps each item.",
        "done": True,
    }
    assert provider_names == ["ollama"]
    assert api_key not in response.text


def test_generation_endpoint_maps_provider_errors_to_safe_contract(monkeypatch):
    from forge.providers import ProviderTimeoutError

    class FakeProvider:
        async def generate(self, payload):
            raise ProviderTimeoutError("private upstream detail")

    monkeypatch.setattr(
        "forge.api.routes.generation.get_provider",
        lambda name, settings: FakeProvider(),
    )
    api_key = "a" * 40
    response = request(
        create_app(ForgeSettings(_env_file=None, forge_api_key=api_key)),
        "POST",
        "/api/v1/generate",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body={
            "model": "qwen2.5-coder:7b",
            "messages": [{"role": "user", "content": "Hello"}],
        },
        raise_app_exceptions=False,
    )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "provider_timeout"
    assert "private upstream detail" not in response.text


def test_generation_endpoint_rejects_unconfigured_default_provider():
    api_key = "a" * 40
    response = request(
        create_app(
            ForgeSettings(
                _env_file=None,
                forge_api_key=api_key,
                default_model="gemini",
            )
        ),
        "POST",
        "/api/v1/generate",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body={
            "model": "gemini-2.5-pro",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"


@pytest.mark.parametrize(
    ("provider_name", "setting_name"),
    [("gemini", "gemini_api_key"), ("openrouter", "openrouter_api_key")],
)
def test_generation_endpoint_dispatches_to_configured_cloud_provider(
    monkeypatch,
    provider_name: str,
    setting_name: str,
):
    from forge.providers.models import GenerationResult

    class FakeProvider:
        async def generate(self, payload):
            return GenerationResult(model=payload.model, content="Cloud response", done=True)

    selected_names = []

    def fake_get_provider(name, settings):
        selected_names.append(name)
        return FakeProvider()

    monkeypatch.setattr("forge.api.routes.generation.get_provider", fake_get_provider)
    api_key = "a" * 40
    response = request(
        create_app(
            ForgeSettings(
                _env_file=None,
                forge_api_key=api_key,
                default_model=provider_name,
                **{setting_name: "cloud-provider-key"},
            )
        ),
        "POST",
        "/api/v1/generate",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body={
            "model": "provider/model",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["content"] == "Cloud response"
    assert selected_names == [provider_name]


def test_agent_task_requires_an_api_key_and_configured_workspace(tmp_path):
    payload = {
        "model": "qwen2.5-coder:7b",
        "messages": [{"role": "user", "content": "Summarize the repository."}],
    }
    api_key = "a" * 40
    unconfigured = request(
        create_app(ForgeSettings(_env_file=None, forge_api_key=api_key)),
        "POST",
        "/api/v1/agent/tasks",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body=payload,
    )
    unauthorized = request(
        create_app(
            ForgeSettings(
                _env_file=None,
                forge_api_key=api_key,
                agent_workspace_root=tmp_path,
            )
        ),
        "POST",
        "/api/v1/agent/tasks",
        json_body=payload,
    )

    assert unconfigured.status_code == 503
    assert unauthorized.status_code == 401


def test_agent_task_uses_only_configured_read_only_workspace(monkeypatch, tmp_path):
    from forge.providers.models import GenerationResult

    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=private", encoding="utf-8")
    (tmp_path / "FORGE.md").write_text("Use focused answers.", encoding="utf-8")

    captured_messages = []

    class FakeProvider:
        async def generate(self, payload):
            captured_messages.extend(payload.messages)
            return GenerationResult(model=payload.model, content="Repository summary", done=True)

    monkeypatch.setattr("forge.api.routes.agent.get_provider", lambda name, settings: FakeProvider())
    api_key = "a" * 40
    response = request(
        create_app(
            ForgeSettings(
                _env_file=None,
                forge_api_key=api_key,
                agent_workspace_root=tmp_path,
            )
        ),
        "POST",
        "/api/v1/agent/tasks",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body={
            "model": "qwen2.5-coder:7b",
            "messages": [{"role": "user", "content": "Explain main.py"}],
            "context_paths": ["main.py"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "generation": {
            "model": "qwen2.5-coder:7b",
            "content": "Repository summary",
            "done": True,
        },
        "workspace_root": str(tmp_path.resolve()),
        "context_paths": ["main.py"],
        "read_only": True,
    }
    context = captured_messages[0].content
    assert "File: main.py\nprint('hello')" in context
    assert "Use focused answers." in context
    assert "TOKEN=private" not in context
    assert (tmp_path / "main.py").read_text(encoding="utf-8") == "print('hello')\n"


def test_agent_task_rejects_context_paths_outside_workspace(monkeypatch, tmp_path):
    outside_path = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside_path.write_text("private", encoding="utf-8")
    monkeypatch.setattr("forge.api.routes.agent.get_provider", lambda name, settings: None)
    api_key = "a" * 40

    response = request(
        create_app(
            ForgeSettings(
                _env_file=None,
                forge_api_key=api_key,
                agent_workspace_root=tmp_path,
            )
        ),
        "POST",
        "/api/v1/agent/tasks",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body={
            "model": "qwen2.5-coder:7b",
            "messages": [{"role": "user", "content": "Read a file"}],
            "context_paths": ["../outside.txt"],
        },
    )

    assert response.status_code == 422
    assert "private" not in response.text


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
    generation_preflight = request(
        app,
        "OPTIONS",
        "/api/v1/generate",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert preflight.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert "Access-Control-Allow-Credentials" not in preflight.headers
    assert "GET" in preflight.headers["Access-Control-Allow-Methods"]
    assert "POST" in generation_preflight.headers["Access-Control-Allow-Methods"]
    assert (
        "authorization"
        in generation_preflight.headers["Access-Control-Allow-Headers"].lower()
    )
    assert response.headers["Access-Control-Expose-Headers"] == "X-Request-ID"


def test_production_settings_require_explicit_secure_cors_origins():
    api_key = "a" * 40
    with pytest.raises(ValidationError, match="requires FORGE_API_KEY"):
        ForgeSettings(
            _env_file=None,
            environment="production",
            cors_origins="https://forge.example.com",
        )

    with pytest.raises(ValidationError, match="Production CORS origins"):
        ForgeSettings(
            _env_file=None,
            environment="production",
            forge_api_key=api_key,
        )

    settings = ForgeSettings(
        _env_file=None,
        environment="production",
        cors_origins="https://forge.example.com",
        forge_api_key=api_key,
    )
    assert settings.allowed_origins == ["https://forge.example.com"]


def test_generation_api_key_is_optional_in_development_but_strong_when_set():
    assert ForgeSettings(_env_file=None, forge_api_key="").forge_api_key is None
    with pytest.raises(ValidationError, match="at least 32 characters"):
        ForgeSettings(_env_file=None, forge_api_key="too-short")


@pytest.mark.parametrize(
    ("setting_name", "value"),
    [("rate_limit_requests", 0), ("rate_limit_window_seconds", 0)],
)
def test_settings_require_positive_rate_limit_values(setting_name: str, value: int):
    with pytest.raises(ValidationError, match="RATE_LIMIT"):
        ForgeSettings(_env_file=None, **{setting_name: value})


def test_generation_api_key_uses_expected_environment_variable(monkeypatch):
    api_key = "a" * 40
    monkeypatch.setenv("FORGE_API_KEY", api_key)

    settings = ForgeSettings(_env_file=None)

    assert settings.forge_api_key is not None
    assert settings.forge_api_key.get_secret_value() == api_key
    assert api_key not in repr(settings)


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
    generation = schema["paths"]["/api/v1/generate"]["post"]

    assert responses["404"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorResponse"
    )
    assert responses["422"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorResponse"
    )
    assert responses["500"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorResponse"
    )
    assert generation["security"] == [{"ForgeBearer": []}]
    assert "ForgeBearer" in schema["components"]["securitySchemes"]


def test_agent_tasks_stream_sse_success_and_persists_session(monkeypatch, tmp_path):
    from forge.providers.models import GenerationChunk
    from forge.sessions import SessionStore

    (tmp_path / "main.py").write_text("print('hello')", encoding="utf-8")
    store = SessionStore(tmp_path)
    session = store.create("Streaming chat")

    class FakeStreamingProvider:
        async def stream_generate(self, payload):
            yield GenerationChunk(content="Streamed ", done=False, model=payload.model)
            yield GenerationChunk(content="answer.", done=True, model=payload.model)

    monkeypatch.setattr("forge.api.routes.agent.get_provider", lambda name, settings: FakeStreamingProvider())
    api_key = "a" * 40
    app = create_app(
        ForgeSettings(
            _env_file=None,
            forge_api_key=api_key,
            agent_workspace_root=tmp_path,
        )
    )

    response = request(
        app,
        "POST",
        "/api/v1/agent/tasks/stream",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body={
            "model": "qwen2.5-coder:7b",
            "messages": [{"role": "user", "content": "Explain main.py"}],
            "context_paths": ["main.py"],
            "session_id": session.id,
        },
    )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    lines = response.text.strip().split("\n\n")
    events = [json.loads(line.removeprefix("data: ").strip()) for line in lines if line.startswith("data: ")]
    assert len(events) == 3
    assert events[0]["content"] == "Streamed "
    assert events[0]["done"] is False
    assert events[1]["content"] == "answer."
    assert events[1]["done"] is False
    assert events[2]["content"] == ""
    assert events[2]["done"] is True
    assert events[2]["session_id"] == session.id

    # Check turns persisted
    saved = store.get(session.id)
    assert len(saved.turns) == 2
    assert saved.turns[0].content == "Explain main.py"
    assert saved.turns[1].content == "Streamed answer."


def test_agent_tasks_stream_auth_and_config_validation(tmp_path):
    api_key = "a" * 40
    payload = {
        "model": "qwen2.5-coder:7b",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    unconfigured = request(
        create_app(ForgeSettings(_env_file=None, forge_api_key=api_key)),
        "POST",
        "/api/v1/agent/tasks/stream",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body=payload,
    )
    unauthorized = request(
        create_app(
            ForgeSettings(
                _env_file=None,
                forge_api_key=api_key,
                agent_workspace_root=tmp_path,
            )
        ),
        "POST",
        "/api/v1/agent/tasks/stream",
        json_body=payload,
    )

    assert unconfigured.status_code == 503
    assert unauthorized.status_code == 401


def test_agent_tasks_stream_rejects_outside_path(tmp_path):
    api_key = "a" * 40
    app = create_app(
        ForgeSettings(
            _env_file=None,
            forge_api_key=api_key,
            agent_workspace_root=tmp_path,
        )
    )
    response = request(
        app,
        "POST",
        "/api/v1/agent/tasks/stream",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body={
            "model": "qwen2.5-coder:7b",
            "messages": [{"role": "user", "content": "Read outside"}],
            "context_paths": ["../outside.txt"],
        },
    )
    assert response.status_code == 422


def test_agent_tasks_stream_session_not_found(tmp_path):
    api_key = "a" * 40
    app = create_app(
        ForgeSettings(
            _env_file=None,
            forge_api_key=api_key,
            agent_workspace_root=tmp_path,
        )
    )
    response = request(
        app,
        "POST",
        "/api/v1/agent/tasks/stream",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body={
            "model": "qwen2.5-coder:7b",
            "messages": [{"role": "user", "content": "Hello"}],
            "session_id": "f" * 32,
        },
    )
    assert response.status_code == 404


def test_agent_tasks_stream_fallback_non_streaming_provider(monkeypatch, tmp_path):
    from forge.providers.models import GenerationResult

    (tmp_path / "main.py").write_text("print('hello')", encoding="utf-8")

    class FakeNonStreamingProvider:
        async def generate(self, payload):
            return GenerationResult(
                model=payload.model,
                content="Non-streaming answer.",
                done=True,
            )

    monkeypatch.setattr("forge.api.routes.agent.get_provider", lambda name, settings: FakeNonStreamingProvider())
    api_key = "a" * 40
    app = create_app(
        ForgeSettings(
            _env_file=None,
            forge_api_key=api_key,
            agent_workspace_root=tmp_path,
        )
    )

    response = request(
        app,
        "POST",
        "/api/v1/agent/tasks/stream",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body={
            "model": "qwen2.5-coder:7b",
            "messages": [{"role": "user", "content": "Explain main.py"}],
            "context_paths": ["main.py"],
        },
    )

    assert response.status_code == 200
    lines = response.text.strip().split("\n\n")
    events = [json.loads(line.removeprefix("data: ").strip()) for line in lines if line.startswith("data: ")]
    assert len(events) == 2
    assert events[0]["content"] == "Non-streaming answer."
    assert events[0]["done"] is False
    assert events[1]["content"] == ""
    assert events[1]["done"] is True


def test_agent_tasks_stream_emits_error_event_on_provider_error(monkeypatch, tmp_path):
    from forge.providers.errors import ProviderUnavailableError

    (tmp_path / "main.py").write_text("print('hello')", encoding="utf-8")

    class FailingStreamingProvider:
        async def stream_generate(self, payload):
            if True:
                raise ProviderUnavailableError
            yield None

    monkeypatch.setattr("forge.api.routes.agent.get_provider", lambda name, settings: FailingStreamingProvider())
    api_key = "a" * 40
    app = create_app(
        ForgeSettings(
            _env_file=None,
            forge_api_key=api_key,
            agent_workspace_root=tmp_path,
        )
    )

    response = request(
        app,
        "POST",
        "/api/v1/agent/tasks/stream",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body={
            "model": "qwen2.5-coder:7b",
            "messages": [{"role": "user", "content": "Explain main.py"}],
            "context_paths": ["main.py"],
        },
    )

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "provider_unavailable" in response.text
    assert "The configured model provider is unavailable." in response.text
