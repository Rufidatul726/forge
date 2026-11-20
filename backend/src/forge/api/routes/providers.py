from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from forge.config import ForgeSettings

router = APIRouter(prefix="/providers", tags=["providers"])


class ProviderStatus(BaseModel):
    name: Literal["ollama", "gemini", "openrouter"]
    kind: Literal["local", "cloud"]
    configured: bool


class ProvidersResponse(BaseModel):
    default_model: str
    providers: list[ProviderStatus]


@router.get("", response_model=ProvidersResponse)
def list_providers() -> ProvidersResponse:
    settings = ForgeSettings()
    return ProvidersResponse(
        default_model=settings.default_model,
        providers=[
            ProviderStatus(name="ollama", kind="local", configured=bool(settings.ollama_host)),
            ProviderStatus(name="gemini", kind="cloud", configured=bool(settings.gemini_api_key)),
            ProviderStatus(
                name="openrouter",
                kind="cloud",
                configured=bool(settings.openrouter_api_key),
            ),
        ],
    )
