from forge.config import ForgeSettings
from forge.providers.errors import (
    ProviderError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderResponseTooLargeError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from forge.providers.ollama import OllamaProvider
from forge.providers.protocol import ModelProvider


def get_provider(name: str, settings: ForgeSettings | None = None) -> ModelProvider:
    active_settings = settings or ForgeSettings()
    if name == "ollama":
        if not active_settings.ollama_host:
            raise ProviderUnavailableError
        return OllamaProvider(active_settings.ollama_host)
    raise ValueError(f"Unsupported model provider: {name}")


__all__ = [
    "ModelProvider",
    "OllamaProvider",
    "ProviderError",
    "ProviderRequestError",
    "ProviderResponseError",
    "ProviderResponseTooLargeError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "get_provider",
]
