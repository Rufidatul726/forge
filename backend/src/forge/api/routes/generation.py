import hmac

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from forge.api.errors import ERROR_RESPONSES
from forge.config import ForgeSettings, get_settings
from forge.providers import get_provider
from forge.providers.models import GenerationRequest, GenerationResult

router = APIRouter(tags=["generation"])
api_key_scheme = HTTPBearer(
    scheme_name="ForgeBearer",
    description="Server-side Forge API key.",
    auto_error=False,
)


async def require_generation_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(api_key_scheme),
    settings: ForgeSettings = Depends(get_settings),
) -> None:
    configured_key = settings.forge_api_key
    if configured_key is None:
        raise HTTPException(status_code=503)

    if (
        credentials is None
        or not hmac.compare_digest(
            credentials.credentials.encode("utf-8"),
            configured_key.get_secret_value().encode("utf-8"),
        )
    ):
        raise HTTPException(status_code=401)


@router.post(
    "/generate",
    response_model=GenerationResult,
    responses=ERROR_RESPONSES,
)
async def generate(
    payload: GenerationRequest,
    settings: ForgeSettings = Depends(get_settings),
    _: None = Depends(require_generation_api_key),
) -> GenerationResult:
    if settings.default_model != "ollama":
        raise HTTPException(status_code=501)
    provider = get_provider("ollama", settings)
    return await provider.generate(payload)
