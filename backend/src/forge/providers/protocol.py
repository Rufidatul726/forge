from typing import Protocol

from forge.providers.models import GenerationRequest, GenerationResult


class ModelProvider(Protocol):
    name: str

    async def list_models(self) -> list[str]: ...

    async def generate(self, request: GenerationRequest) -> GenerationResult: ...
