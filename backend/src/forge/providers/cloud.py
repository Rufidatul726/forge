import json
from collections.abc import AsyncIterator, Mapping
from typing import Any
from uuid import uuid4

import httpx

from forge.providers.errors import (
    ProviderRequestError,
    ProviderResponseError,
    ProviderResponseTooLargeError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from forge.providers.models import (
    AssistantMessage,
    ChatMessage,
    GenerationChunk,
    GenerationRequest,
    GenerationResult,
    ProviderResponse,
    ToolCall,
)
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

    async def generate(self, request: GenerationRequest) -> ProviderResponse:
        contents: list[dict[str, Any]] = []
        system_parts: list[dict[str, str]] = []
        for message in request.messages:
            if message.role == "system":
                system_parts.append({"text": message.content})
            elif message.role == "tool":
                contents.append({
                    "role": "user",
                    "parts": [{
                        "functionResponse": {
                            "name": message.name or "tool",
                            "response": {"result": message.content},
                        }
                    }],
                })
            elif message.tool_calls:
                parts: list[dict[str, Any]] = []
                if message.content:
                    parts.append({"text": message.content})
                for tc in message.tool_calls:
                    parts.append({"functionCall": {"name": tc.name, "args": tc.arguments}})
                contents.append({"role": "model", "parts": parts})
            else:
                contents.append(
                    {"role": "model" if message.role == "assistant" else "user", "parts": [{"text": message.content}]}
                )

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": request.max_tokens},
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}
        if request.tools:
            function_declarations = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                for tool in request.tools
            ]
            payload["tools"] = [{"functionDeclarations": function_declarations}]

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

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            if "text" in part and isinstance(part["text"], str):
                text_parts.append(part["text"])
            if "functionCall" in part and isinstance(part["functionCall"], Mapping):
                fc = part["functionCall"]
                fn_name = fc.get("name")
                if isinstance(fn_name, str) and fn_name:
                    fn_args = fc.get("args") or {}
                    if not isinstance(fn_args, Mapping):
                        fn_args = {}
                    tool_calls.append(ToolCall(id=uuid4().hex, name=fn_name, arguments=dict(fn_args)))

        text = "".join(text_parts)
        if not text and not tool_calls:
            raise ProviderResponseError

        assistant_message = AssistantMessage(
            role="assistant",
            content=text,
            tool_calls=tool_calls,
        )
        return ProviderResponse(
            model=request.model,
            content=text,
            message=assistant_message,
            tool_calls=tool_calls,
            done=True,
        )

    async def stream_generate(
        self, request: GenerationRequest
    ) -> AsyncIterator[GenerationChunk]:
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

        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "POST",
                    f"/v1beta/models/{request.model}:streamGenerateContent?alt=sse",
                    headers={"x-goog-api-key": self._api_key},
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        raise ProviderRequestError
                    total_bytes = 0
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        line_bytes = len(line.encode("utf-8"))
                        total_bytes += line_bytes
                        if total_bytes > self._max_response_bytes:
                            raise ProviderResponseTooLargeError
                        if not line.startswith("data:"):
                            continue
                        data_str = line.removeprefix("data:").strip()
                        if not data_str:
                            continue
                        try:
                            data = json.loads(data_str)
                        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                            raise ProviderResponseError from exc
                        if not isinstance(data, Mapping):
                            raise ProviderResponseError
                        if "error" in data:
                            raise ProviderRequestError
                        candidates = data.get("candidates")
                        if isinstance(candidates, list) and candidates:
                            candidate = candidates[0]
                            if isinstance(candidate, Mapping):
                                content = candidate.get("content")
                                if isinstance(content, Mapping):
                                    parts = content.get("parts")
                                    if isinstance(parts, list):
                                        text = "".join(
                                            p.get("text", "")
                                             for p in parts
                                            if isinstance(p, Mapping)
                                        )
                                        if text:
                                            yield GenerationChunk(
                                                content=text,
                                                done=False,
                                                model=request.model,
                                            )
                    yield GenerationChunk(content="", done=True, model=request.model)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError from exc


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

    async def generate(self, request: GenerationRequest) -> ProviderResponse:
        messages_payload: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "tool":
                messages_payload.append({
                    "role": "tool",
                    "content": message.content,
                    "tool_call_id": message.tool_call_id or "",
                })
            elif message.tool_calls:
                messages_payload.append({
                    "role": "assistant",
                    "content": message.content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments) if isinstance(tc.arguments, dict) else str(tc.arguments),
                            },
                        }
                        for tc in message.tool_calls
                    ],
                })
            else:
                messages_payload.append({
                    "role": message.role,
                    "content": message.content,
                })

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages_payload,
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]

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
        content = message.get("content") or ""
        model = response.get("model", request.model)

        raw_tool_calls = message.get("tool_calls")
        tool_calls: list[ToolCall] = []
        if isinstance(raw_tool_calls, list):
            for raw_call in raw_tool_calls:
                if not isinstance(raw_call, Mapping):
                    raise ProviderResponseError
                fn = raw_call.get("function")
                if not isinstance(fn, Mapping):
                    raise ProviderResponseError
                fn_name = fn.get("name")
                if not isinstance(fn_name, str) or not fn_name:
                    raise ProviderResponseError
                fn_args = fn.get("arguments", {})
                if isinstance(fn_args, str):
                    try:
                        fn_args = json.loads(fn_args)
                    except json.JSONDecodeError as exc:
                        raise ProviderResponseError from exc
                if not isinstance(fn_args, Mapping):
                    raise ProviderResponseError
                call_id = raw_call.get("id") or uuid4().hex
                tool_calls.append(ToolCall(id=str(call_id), name=fn_name, arguments=dict(fn_args)))

        if not content and not tool_calls:
            raise ProviderResponseError

        assistant_message = AssistantMessage(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
        )
        return ProviderResponse(
            model=model,
            content=content,
            message=assistant_message,
            tool_calls=tool_calls,
            done=True,
        )

    async def stream_generate(
        self, request: GenerationRequest
    ) -> AsyncIterator[GenerationChunk]:
        payload = {
            "model": request.model,
            "messages": [message.model_dump(exclude_none=True) for message in request.messages],
            "max_tokens": request.max_tokens,
            "stream": True,
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "POST",
                    "/chat/completions",
                    headers=self._headers,
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        raise ProviderRequestError
                    total_bytes = 0
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        line_bytes = len(line.encode("utf-8"))
                        total_bytes += line_bytes
                        if total_bytes > self._max_response_bytes:
                            raise ProviderResponseTooLargeError
                        if not line.startswith("data:"):
                            continue
                        data_str = line.removeprefix("data:").strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                            raise ProviderResponseError from exc
                        if not isinstance(data, Mapping):
                            raise ProviderResponseError
                        if "error" in data:
                            raise ProviderRequestError
                        choices = data.get("choices")
                        if isinstance(choices, list) and choices:
                            choice = choices[0]
                            if isinstance(choice, Mapping):
                                delta = choice.get("delta")
                                if isinstance(delta, Mapping):
                                    text = delta.get("content")
                                    if isinstance(text, str) and text:
                                        yield GenerationChunk(
                                            content=text,
                                            done=False,
                                            model=data.get("model", request.model),
                                        )
                    yield GenerationChunk(content="", done=True, model=request.model)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError from exc
