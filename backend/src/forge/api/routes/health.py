from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from forge.api.errors import ERROR_RESPONSES

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


@router.get("/health", response_model=HealthResponse, responses=ERROR_RESPONSES)
def health_check() -> HealthResponse:
    return HealthResponse(status="ok", service="forge-api")
