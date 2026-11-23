from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from forge.api.errors import ERROR_RESPONSES
from forge.config import ForgeSettings, get_settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


class ReadinessResponse(BaseModel):
    status: Literal["ready"]
    workspace: bool
    api_key: bool


@router.get("/health", response_model=HealthResponse, responses=ERROR_RESPONSES)
def health_check() -> HealthResponse:
    return HealthResponse(status="ok", service="forge-api")


@router.get("/ready", response_model=ReadinessResponse, responses=ERROR_RESPONSES)
def readiness_check(settings: ForgeSettings = Depends(get_settings)) -> ReadinessResponse:
    workspace_ready = (
        settings.agent_workspace_root is not None
        and settings.agent_workspace_root.is_dir()
    )
    if settings.auth_mode == "oidc":
        auth_ready = bool(settings.oidc_issuer and settings.oidc_audience)
    elif settings.auth_mode == "api_key_or_oidc":
        auth_ready = settings.forge_api_key is not None or bool(
            settings.oidc_issuer and settings.oidc_audience
        )
    else:
        auth_ready = settings.forge_api_key is not None
    if not workspace_ready or not auth_ready:
        raise HTTPException(status_code=503)
    return ReadinessResponse(
        status="ready",
        workspace=True,
        api_key=auth_ready,
    )
