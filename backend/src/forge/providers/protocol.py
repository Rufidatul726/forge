from collections.abc import AsyncIterator
from typing import Protocol

from forge.providers.models import (
    GenerationChunk,
    GenerationRequest,
    ProviderResponse,
)


class ModelProvider(Protocol):
    name: str

    async def list_models(self) -> list[str]: ...

    async def generate(self, request: GenerationRequest) -> ProviderResponse: ...

    async def stream_generate(
        self, request: GenerationRequest
    ) -> AsyncIterator[GenerationChunk]: ...
