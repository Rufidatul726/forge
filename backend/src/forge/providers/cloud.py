import json
from collections.abc import Mapping
from typing import Any

import httpx

from forge.providers.errors import (
    ProviderRequestError,
    ProviderResponseError,
    ProviderResponseTooLargeError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from forge.providers.models import GenerationRequest, GenerationResult
from forge.providers.ollama import (
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_MODELS,
)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class _CloudProvider:
    name: str

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Provider API key must not be blank")
        if timeout_seconds <= 0 or max_response_bytes <= 0:
            raise ValueError("Timeout and response-size limits must be positive")

        self._api_key = api_key
        self._base_url = base_url
        self._timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0))
        self._max_response_bytes = max_response_bytes
        self._transport = transport

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    method,
                    path,
                    headers=headers,
                    json=payload,
                ) as response:
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


class GeminiProvider(_CloudProvider):
    name = "gemini"

    def __init__(self, api_key: str, **kwargs: Any) -> None:
        super().__init__(api_key, base_url=GEMINI_BASE_URL, **kwargs)

    async def list_models(self) -> list[str]:
        payload = await self._request_json(
            "GET",
            "/v1beta/models",
            headers={"x-goog-api-key": self._api_key},
        )
        models = payload.get("models")
        if not isinstance(models, list) or len(models) > MAX_MODELS:
            raise ProviderResponseError

        names: list[str] = []
        for item in models:
            if not isinstance(item, Mapping):
                raise ProviderResponseError
            name = item.get("name")
            if not isinstance(name, str) or not name.startswith("models/"):
                raise ProviderResponseError
            model = name.removeprefix("models/")
            if not model or len(model) > 255:
                raise ProviderResponseError
            names.append(model)
        return names

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        contents: list[dict[str, Any]] = []
        system_parts: list[dict[str, str]] = []
        for message in request.messages:
            part = {"text": message.content}
            if message.role == "system":
                system_parts.append(part)
            else:
                contents.append(
                    {"role": "model" if message.role == "assistant" else "user", "parts": [part]}
                )

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": request.max_tokens},
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}
        response = await self._request_json(
            "POST",
            f"/v1beta/models/{request.model}:generateContent",
            headers={"x-goog-api-key": self._api_key},
            payload=payload,
        )
        candidates = response.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ProviderResponseError
        candidate = candidates[0]
        if not isinstance(candidate, Mapping):
            raise ProviderResponseError
        content = candidate.get("content")
        if not isinstance(content, Mapping) or content.get("role") != "model":
            raise ProviderResponseError
        parts = content.get("parts")
        if not isinstance(parts, list):
            raise ProviderResponseError
        text = "".join(part.get("text", "") for part in parts if isinstance(part, Mapping))
        if not text:
            raise ProviderResponseError
        return GenerationResult(model=request.model, content=text, done=True)


class OpenRouterProvider(_CloudProvider):
    name = "openrouter"

    def __init__(self, api_key: str, **kwargs: Any) -> None:
        super().__init__(api_key, base_url=OPENROUTER_BASE_URL, **kwargs)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def list_models(self) -> list[str]:
        payload = await self._request_json("GET", "/models", headers=self._headers)
        models = payload.get("data")
        if not isinstance(models, list) or len(models) > MAX_MODELS:
            raise ProviderResponseError

        names: list[str] = []
        for item in models:
            if not isinstance(item, Mapping):
                raise ProviderResponseError
            name = item.get("id")
            if not isinstance(name, str) or not name.strip() or len(name) > 255:
                raise ProviderResponseError
            names.append(name.strip())
        return names

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        payload = {
            "model": request.model,
            "messages": [message.model_dump() for message in request.messages],
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        response = await self._request_json(
            "POST",
            "/chat/completions",
            headers=self._headers,
            payload=payload,
        )
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderResponseError
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise ProviderResponseError
        message = choice.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            raise ProviderResponseError
        content = message.get("content")
        model = response.get("model", request.model)
        if not isinstance(content, str) or not content or not isinstance(model, str):
            raise ProviderResponseError
        return GenerationResult(model=model, content=content, done=True)
