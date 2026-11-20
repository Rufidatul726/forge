import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from forge.providers.errors import (
    ProviderRequestError,
    ProviderResponseError,
    ProviderResponseTooLargeError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from forge.providers.models import GenerationRequest, GenerationResult

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_MODELS = 1_000


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Ollama host must be an HTTP(S) origin without credentials or a path")
        if timeout_seconds <= 0 or max_response_bytes <= 0:
            raise ValueError("Timeout and response-size limits must be positive")

        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("Ollama host contains an invalid port") from exc

        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0))
        self._max_response_bytes = max_response_bytes
        self._transport = transport

    async def list_models(self) -> list[str]:
        payload = await self._request_json("GET", "/api/tags")
        models = payload.get("models")
        if not isinstance(models, list) or len(models) > MAX_MODELS:
            raise ProviderResponseError

        names: list[str] = []
        for item in models:
            if not isinstance(item, Mapping):
                raise ProviderResponseError
            name = item.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 255:
                raise ProviderResponseError
            names.append(name.strip())
        return names

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        payload = await self._request_json(
            "POST",
            "/api/chat",
            {
                "model": request.model,
                "messages": [message.model_dump() for message in request.messages],
                "stream": False,
                "options": {"num_predict": request.max_tokens},
            },
        )
        message = payload.get("message")
        if not isinstance(message, Mapping):
            raise ProviderResponseError

        role = message.get("role")
        content = message.get("content")
        model = payload.get("model", request.model)
        done = payload.get("done")
        if (
            role != "assistant"
            or not isinstance(content, str)
            or not isinstance(model, str)
            or not isinstance(done, bool)
        ):
            raise ProviderResponseError

        return GenerationResult(model=model, content=content, done=done)

    async def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                async with client.stream(method, path, json=payload) as response:
                    if response.status_code >= 400:
                        raise ProviderRequestError
                    body = await self._read_limited(response)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError from exc

        try:
            decoded = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderResponseError from exc
        if not isinstance(decoded, dict):
            raise ProviderResponseError
        return decoded

    async def _read_limited(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total_bytes = 0
        async for chunk in response.aiter_bytes():
            total_bytes += len(chunk)
            if total_bytes > self._max_response_bytes:
                raise ProviderResponseTooLargeError
            chunks.append(chunk)
        return b"".join(chunks)
