from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from forge.api.auth import AuthenticationError, authenticate_bearer
from forge.api.errors import ERROR_RESPONSES
from forge.config import ForgeSettings, get_settings
from forge.providers import get_provider
from forge.providers.models import GenerationRequest, GenerationResult

router = APIRouter(tags=["generation"])
api_key_scheme = HTTPBearer(
    scheme_name="ForgeBearer",
    description="Bearer authentication via Forge API key or OIDC access token.",
    auto_error=False,
)


async def require_generation_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(api_key_scheme),
    settings: ForgeSettings = Depends(get_settings),
) -> None:
    configured_key = settings.forge_api_key
    if settings.auth_mode == "api_key" and configured_key is None:
        raise HTTPException(status_code=503)
    if settings.auth_mode in {"oidc", "api_key_or_oidc"} and (
        settings.oidc_issuer is None or settings.oidc_audience is None
    ):
        raise HTTPException(status_code=503)

    if credentials is None:
        raise HTTPException(status_code=401)
    try:
        await authenticate_bearer(credentials.credentials, settings)
    except AuthenticationError as exc:
        code = str(exc)
        if code in {"insufficient_scope", "insufficient_role"}:
            raise HTTPException(
                status_code=403,
                detail=f"Token lacks required {code.replace('insufficient_', '')}",
            ) from exc
        raise HTTPException(status_code=401)


require_authentication = require_generation_api_key


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
    if settings.default_model not in {"ollama", "gemini", "openrouter"}:
        raise HTTPException(status_code=501)
    provider = get_provider(settings.default_model, settings)
    return await provider.generate(payload)
