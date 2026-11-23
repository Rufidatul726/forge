from forge.config import ForgeSettings
from forge.providers.errors import (
    ProviderError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderResponseTooLargeError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from forge.providers.cloud import GeminiProvider, OpenRouterProvider
from forge.providers.ollama import OllamaProvider
from forge.providers.models import (
    AssistantMessage,
    ChatMessage,
    GenerationChunk,
    GenerationRequest,
    GenerationResult,
    ProviderResponse,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from forge.providers.protocol import ModelProvider


def get_provider(name: str, settings: ForgeSettings | None = None) -> ModelProvider:
    active_settings = settings or ForgeSettings()
    if name == "ollama":
        if not active_settings.ollama_host:
            raise ProviderUnavailableError
        return OllamaProvider(active_settings.ollama_host)
    if name == "gemini":
        if active_settings.gemini_api_key is None:
            raise ProviderUnavailableError
        return GeminiProvider(active_settings.gemini_api_key.get_secret_value())
    if name == "openrouter":
        if active_settings.openrouter_api_key is None:
            raise ProviderUnavailableError
        return OpenRouterProvider(active_settings.openrouter_api_key.get_secret_value())
    raise ValueError(f"Unsupported model provider: {name}")


__all__ = [
    "AssistantMessage",
    "ChatMessage",
    "GeminiProvider",
    "GenerationChunk",
    "GenerationRequest",
    "GenerationResult",
    "ModelProvider",
    "OllamaProvider",
    "OpenRouterProvider",
    "ProviderError",
    "ProviderRequestError",
    "ProviderResponseError",
    "ProviderResponseTooLargeError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "ProviderResponse",
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
    "get_provider",
]
