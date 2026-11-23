import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from forge.providers import (
    GeminiProvider,
    OllamaProvider,
    OpenRouterProvider,
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


def gemini_provider(handler) -> GeminiProvider:
    return GeminiProvider("gemini-test-key", transport=httpx.MockTransport(handler))


def openrouter_provider(handler) -> OpenRouterProvider:
    return OpenRouterProvider("openrouter-test-key", transport=httpx.MockTransport(handler))


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


def test_gemini_lists_models_and_uses_server_side_key():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1beta/models"
        assert request.headers["x-goog-api-key"] == "gemini-test-key"
        return httpx.Response(200, json={"models": [{"name": "models/gemini-2.5-flash"}]})

    assert asyncio.run(gemini_provider(respond).list_models()) == ["gemini-2.5-flash"]


def test_gemini_generates_with_neutral_messages():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1beta/models/gemini-2.5-flash:generateContent"
        assert json.loads(request.content) == {
            "systemInstruction": {"parts": [{"text": "Be concise."}]},
            "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
            "generationConfig": {"maxOutputTokens": 128},
        }
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": "Hi there."}]}}
                ]
            },
        )

    result = asyncio.run(
        gemini_provider(respond).generate(
            GenerationRequest(
                model="gemini-2.5-flash",
                messages=[
                    ChatMessage(role="system", content="Be concise."),
                    ChatMessage(role="user", content="Hello"),
                ],
                max_tokens=128,
            )
        )
    )

    assert result.content == "Hi there."
    assert result.done is True


def test_openrouter_lists_models_and_generates_with_bearer_key():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer openrouter-test-key"
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-4.1-mini"}]})
        assert request.url.path == "/api/v1/chat/completions"
        assert json.loads(request.content) == {
            "model": "openai/gpt-4.1-mini",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 128,
            "stream": False,
        }
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-4.1-mini",
                "choices": [{"message": {"role": "assistant", "content": "Hi there."}}],
            },
        )

    provider_instance = openrouter_provider(respond)
    assert asyncio.run(provider_instance.list_models()) == ["openai/gpt-4.1-mini"]
    result = asyncio.run(
        provider_instance.generate(
            GenerationRequest(
                model="openai/gpt-4.1-mini",
                messages=[ChatMessage(role="user", content="Hello")],
                max_tokens=128,
            )
        )
    )
    assert result.content == "Hi there."
    assert result.done is True


def test_ollama_stream_generate():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content)
        assert payload["stream"] is True
        ndjson = (
            json.dumps({"model": "qwen2.5-coder:7b", "message": {"content": "Hello "}, "done": False})
            + "\n"
            + json.dumps({"model": "qwen2.5-coder:7b", "message": {"content": "world!"}, "done": True})
            + "\n"
        )
        return httpx.Response(200, text=ndjson)

    provider_instance = provider(respond)

    async def collect():
        chunks = []
        async for chunk in provider_instance.stream_generate(
            GenerationRequest(
                model="qwen2.5-coder:7b",
                messages=[ChatMessage(role="user", content="Hi")],
                max_tokens=64,
            )
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(collect())
    assert len(chunks) == 2
    assert chunks[0].content == "Hello "
    assert chunks[0].done is False
    assert chunks[1].content == "world!"
    assert chunks[1].done is True


def test_ollama_stream_too_large():
    def respond(request: httpx.Request) -> httpx.Response:
        line = json.dumps({"model": "m", "message": {"content": "x" * 200}, "done": False}) + "\n"
        return httpx.Response(200, text=line * 10)

    provider_instance = provider(respond, max_response_bytes=100)

    async def collect():
        async for _ in provider_instance.stream_generate(
            GenerationRequest(
                model="m",
                messages=[ChatMessage(role="user", content="Hi")],
                max_tokens=64,
            )
        ):
            pass

    with pytest.raises(ProviderResponseTooLargeError):
        asyncio.run(collect())


def test_gemini_stream_generate():
    def respond(request: httpx.Request) -> httpx.Response:
        assert "streamGenerateContent" in request.url.path
        sse = (
            'data: {"candidates": [{"content": {"parts": [{"text": "Gemini "}],"role": "model"}}]}\n\n'
            'data: {"candidates": [{"content": {"parts": [{"text": "stream"}],"role": "model"}}]}\n\n'
        )
        return httpx.Response(200, text=sse)

    provider_instance = gemini_provider(respond)

    async def collect():
        chunks = []
        async for chunk in provider_instance.stream_generate(
            GenerationRequest(
                model="gemini-2.5-pro",
                messages=[ChatMessage(role="user", content="Hi")],
                max_tokens=64,
            )
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(collect())
    assert len(chunks) == 3
    assert chunks[0].content == "Gemini "
    assert chunks[0].done is False
    assert chunks[1].content == "stream"
    assert chunks[1].done is False
    assert chunks[2].done is True


def test_openrouter_stream_generate():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["stream"] is True
        sse = (
            'data: {"choices": [{"delta": {"content": "OpenRouter "}}]}\n\n'
            'data: {"choices": [{"delta": {"content": "streaming"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=sse)

    provider_instance = openrouter_provider(respond)

    async def collect():
        chunks = []
        async for chunk in provider_instance.stream_generate(
            GenerationRequest(
                model="openai/gpt-4.1-mini",
                messages=[ChatMessage(role="user", content="Hi")],
                max_tokens=64,
            )
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(collect())
    assert len(chunks) == 3
    assert chunks[0].content == "OpenRouter "
    assert chunks[0].done is False
    assert chunks[1].content == "streaming"
    assert chunks[1].done is False
    assert chunks[2].done is True


@pytest.mark.parametrize("status_code", [400, 401, 404, 500])
def test_stream_http_errors_raise_provider_request_error(status_code: int):
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="sensitive upstream text")

    ollama = provider(respond)
    gemini = gemini_provider(respond)
    openrouter = openrouter_provider(respond)

    async def consume(p):
        async for _ in p.stream_generate(
            GenerationRequest(
                model="test-model",
                messages=[ChatMessage(role="user", content="Hi")],
            )
        ):
            pass

    for p in (ollama, gemini, openrouter):
        with pytest.raises(ProviderRequestError) as exc_info:
            asyncio.run(consume(p))
        assert "sensitive upstream text" not in exc_info.value.public_message


@pytest.mark.parametrize(
    ("provider_factory", "body"),
    [
        (provider, '{"error":"upstream failure"}\n'),
        (gemini_provider, 'data: {"error":{"message":"upstream failure"}}\n\n'),
        (openrouter_provider, 'data: {"error":{"message":"upstream failure"}}\n\n'),
    ],
)
def test_stream_error_payloads_raise_provider_request_error(provider_factory, body: str):
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    provider_instance = provider_factory(respond)

    async def consume():
        async for _ in provider_instance.stream_generate(
            GenerationRequest(
                model="test-model",
                messages=[ChatMessage(role="user", content="Hi")],
            )
        ):
            pass

    with pytest.raises(ProviderRequestError):
        asyncio.run(consume())


def test_ollama_stream_yields_terminal_chunk_if_not_sent():
    def respond(_request: httpx.Request) -> httpx.Response:
        ndjson = json.dumps({"model": "m", "message": {"content": "hello"}, "done": False}) + "\n"
        return httpx.Response(200, text=ndjson)

    provider_instance = provider(respond)

    async def collect():
        chunks = []
        async for chunk in provider_instance.stream_generate(
            GenerationRequest(
                model="m",
                messages=[ChatMessage(role="user", content="Hi")],
            )
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(collect())
    assert len(chunks) == 2
    assert chunks[0].content == "hello"
    assert chunks[0].done is False
    assert chunks[1].content == ""
    assert chunks[1].done is True
