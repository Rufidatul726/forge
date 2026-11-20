from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from forge.api.errors import ERROR_RESPONSES
from forge.config import ForgeSettings, get_settings

router = APIRouter(prefix="/providers", tags=["providers"])


class ProviderStatus(BaseModel):
    name: Literal["ollama", "gemini", "openrouter"]
    kind: Literal["local", "cloud"]
    configured: bool


class ProvidersResponse(BaseModel):
    default_model: str
    providers: list[ProviderStatus]


@router.get("", response_model=ProvidersResponse, responses=ERROR_RESPONSES)
def list_providers(settings: ForgeSettings = Depends(get_settings)) -> ProvidersResponse:
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
