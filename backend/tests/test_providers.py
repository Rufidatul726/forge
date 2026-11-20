import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from forge.providers import (
    OllamaProvider,
    ProviderRequestError,
    ProviderResponseError,
    ProviderResponseTooLargeError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from forge.providers.models import ChatMessage, GenerationRequest
from forge.config import ForgeSettings


def provider(handler, *, max_response_bytes: int = 1024) -> OllamaProvider:
    return OllamaProvider(
        "http://127.0.0.1:11434",
        max_response_bytes=max_response_bytes,
        transport=httpx.MockTransport(handler),
    )


def test_list_models_uses_ollama_tags_endpoint():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={"models": [{"name": "qwen2.5-coder:7b"}, {"name": "llama3.2:3b"}]},
        )

    models = asyncio.run(provider(respond).list_models())
    assert models == ["qwen2.5-coder:7b", "llama3.2:3b"]


def test_generate_sends_bounded_non_streaming_request():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/chat"
        assert json.loads(request.content) == {
            "model": "qwen2.5-coder:7b",
            "messages": [{"role": "user", "content": "Explain this function"}],
            "stream": False,
            "options": {"num_predict": 128},
        }
        return httpx.Response(
            200,
            json={
                "model": "qwen2.5-coder:7b",
                "message": {"role": "assistant", "content": "It maps each item."},
                "done": True,
            },
        )

    result = asyncio.run(
        provider(respond).generate(
            GenerationRequest(
                model="qwen2.5-coder:7b",
                messages=[ChatMessage(role="user", content="Explain this function")],
                max_tokens=128,
            )
        )
    )
    assert result.model == "qwen2.5-coder:7b"
    assert result.content == "It maps each item."
    assert result.done is True


def test_provider_http_errors_do_not_expose_upstream_body():
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="sensitive upstream response")

    with pytest.raises(ProviderRequestError) as error:
        asyncio.run(provider(respond).list_models())

    assert error.value.public_message == "The model provider rejected the request."
    assert "sensitive upstream response" not in error.value.public_message


def test_provider_connection_and_timeout_errors_are_mapped():
    def connection_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sensitive connection detail", request=request)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("sensitive timeout detail", request=request)

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider(connection_failure).list_models())
    with pytest.raises(ProviderTimeoutError):
        asyncio.run(provider(timeout).list_models())


def test_provider_response_size_is_bounded():
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 128)

    with pytest.raises(ProviderResponseTooLargeError):
        asyncio.run(provider(respond, max_response_bytes=32).list_models())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"models": [{"name": ""}]}),
        httpx.Response(200, json={"models": "not-a-list"}),
    ],
)
def test_provider_rejects_malformed_model_responses(response: httpx.Response):
    with pytest.raises(ProviderResponseError):
        asyncio.run(provider(lambda request: response).list_models())


def test_generation_request_validates_model_and_token_limits():
    with pytest.raises(ValidationError):
        GenerationRequest(
            model=" ",
            messages=[ChatMessage(role="user", content="Hello")],
        )

    with pytest.raises(ValidationError):
        GenerationRequest(
            model="model",
            messages=[ChatMessage(role="user", content="Hello")],
            max_tokens=8193,
        )


@pytest.mark.parametrize(
    "ollama_host",
    ["ftp://ollama.local", "http://user:password@localhost:11434", "http://localhost/path"],
)
def test_settings_reject_invalid_ollama_hosts(ollama_host: str):
    with pytest.raises(ValidationError):
        ForgeSettings(_env_file=None, ollama_host=ollama_host)


def test_provider_does_not_swallow_cancellation():
    async def cancel(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(provider(cancel).list_models())
