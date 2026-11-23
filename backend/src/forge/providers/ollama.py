import json
from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import urlsplit
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
    ProviderResponse,
    ToolCall,
)

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

    def _format_messages(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        formatted: list[dict[str, Any]] = []
        for message in messages:
            item: dict[str, Any] = {
                "role": message.role,
                "content": message.content,
            }
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments,
                        }
                    }
                    for tc in message.tool_calls
                ]
            formatted.append(item)
        return formatted

    async def generate(self, request: GenerationRequest) -> ProviderResponse:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": self._format_messages(request.messages),
            "stream": False,
            "options": {"num_predict": request.max_tokens},
        }
        if request.tools:
            body["tools"] = [
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

        payload = await self._request_json(
            "POST",
            "/api/chat",
            body,
        )
        message = payload.get("message")
        if not isinstance(message, Mapping):
            raise ProviderResponseError

        role = message.get("role")
        content = message.get("content") or ""
        model = payload.get("model", request.model)
        done = payload.get("done")
        if (
            role != "assistant"
            or not isinstance(content, str)
            or not isinstance(model, str)
            or not isinstance(done, bool)
        ):
            raise ProviderResponseError

        raw_tool_calls = message.get("tool_calls")
        tool_calls: list[ToolCall] = []
        if isinstance(raw_tool_calls, list):
            for raw_call in raw_tool_calls:
                if not isinstance(raw_call, Mapping):
                    continue
                fn = raw_call.get("function")
                if not isinstance(fn, Mapping):
                    continue
                fn_name = fn.get("name")
                if not isinstance(fn_name, str) or not fn_name:
                    continue
                fn_args = fn.get("arguments", {})
                if isinstance(fn_args, str):
                    try:
                        fn_args = json.loads(fn_args)
                    except json.JSONDecodeError:
                        fn_args = {"_raw_malformed_arguments": fn.get("arguments")}
                if not isinstance(fn_args, Mapping):
                    fn_args = {"_raw_arguments": str(fn_args)}
                call_id = raw_call.get("id") or uuid4().hex
                tool_calls.append(ToolCall(id=str(call_id), name=fn_name, arguments=dict(fn_args)))

        if not content and not tool_calls:
            # Allow empty content if tool_calls is present; if both empty, error
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
            done=done,
        )

    async def stream_generate(
        self, request: GenerationRequest
    ) -> AsyncIterator[GenerationChunk]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": self._format_messages(request.messages),
            "stream": True,
            "options": {"num_predict": request.max_tokens},
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
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                async with client.stream("POST", "/api/chat", json=payload) as response:
                    if response.status_code >= 400:
                        raise ProviderRequestError
                    total_bytes = 0
                    has_sent_done = False
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        line_bytes = len(line.encode("utf-8"))
                        total_bytes += line_bytes
                        if total_bytes > self._max_response_bytes:
                            raise ProviderResponseTooLargeError
                        try:
                            item = json.loads(line)
                        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                            raise ProviderResponseError from exc
                        if not isinstance(item, Mapping):
                            raise ProviderResponseError
                        if "error" in item:
                            raise ProviderRequestError
                        message = item.get("message")
                        content = ""
                        chunk_tool_calls: list[ToolCall] = []
                        if isinstance(message, Mapping):
                            text = message.get("content")
                            if isinstance(text, str):
                                content = text
                            raw_tool_calls = message.get("tool_calls")
                            if isinstance(raw_tool_calls, list):
                                for raw_call in raw_tool_calls:
                                    if not isinstance(raw_call, Mapping):
                                        continue
                                    fn = raw_call.get("function")
                                    if not isinstance(fn, Mapping):
                                        continue
                                    fn_name = fn.get("name")
                                    if not isinstance(fn_name, str) or not fn_name:
                                        continue
                                    fn_args = fn.get("arguments", {})
                                    if isinstance(fn_args, str):
                                        try:
                                            fn_args = json.loads(fn_args)
                                        except json.JSONDecodeError:
                                            fn_args = {"_raw_malformed_arguments": fn.get("arguments")}
                                    if not isinstance(fn_args, Mapping):
                                        fn_args = {"_raw_arguments": str(fn_args)}
                                    call_id = raw_call.get("id") or uuid4().hex
                                    chunk_tool_calls.append(
                                        ToolCall(id=str(call_id), name=fn_name, arguments=dict(fn_args))
                                    )
                        model = item.get("model", request.model)
                        done = bool(item.get("done", False))
                        if done:
                            has_sent_done = True
                        yield GenerationChunk(
                            content=content,
                            done=done,
                            model=model,
                            tool_calls=chunk_tool_calls,
                        )
                    if not has_sent_done:
                        yield GenerationChunk(content="", done=True, model=request.model)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError from exc

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
