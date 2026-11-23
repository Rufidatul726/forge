from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from forge.api.errors import ERROR_RESPONSES
from forge.api.routes.generation import require_generation_api_key
from forge.config import ForgeSettings, get_settings
from forge.research.models import ResearchQuery, ResearchReport, ResearchSource
from forge.research.service import ResearchService

router = APIRouter(prefix="/research", tags=["research"])


@router.post(
    "",
    response_model=ResearchReport,
    responses=ERROR_RESPONSES,
)
async def create_research(
    query: ResearchQuery,
    settings: ForgeSettings = Depends(get_settings),
    _: None = Depends(require_generation_api_key),
) -> ResearchReport:
    workspace_root = settings.agent_workspace_root
    if workspace_root is None:
        raise HTTPException(status_code=503, detail="Workspace root not configured")

    service = ResearchService(workspace_root, settings=settings)
    try:
        report = await service.research(query)
        return report
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get(
    "/{research_id}",
    response_model=ResearchReport,
    responses=ERROR_RESPONSES,
)
async def get_research(
    research_id: str,
    settings: ForgeSettings = Depends(get_settings),
    _: None = Depends(require_generation_api_key),
) -> ResearchReport:
    workspace_root = settings.agent_workspace_root
    if workspace_root is None:
        raise HTTPException(status_code=503, detail="Workspace root not configured")

    service = ResearchService(workspace_root, settings=settings)
    report = service.get_report(research_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Research report '{research_id}' not found")
    return report


@router.get(
    "/{research_id}/sources",
    response_model=list[ResearchSource],
    responses=ERROR_RESPONSES,
)
async def get_research_sources(
    research_id: str,
    settings: ForgeSettings = Depends(get_settings),
    _: None = Depends(require_generation_api_key),
) -> list[ResearchSource]:
    workspace_root = settings.agent_workspace_root
    if workspace_root is None:
        raise HTTPException(status_code=503, detail="Workspace root not configured")

    service = ResearchService(workspace_root, settings=settings)
    report = service.get_report(research_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Research report '{research_id}' not found")
    return report.sources
