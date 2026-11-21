from fastapi import APIRouter, Depends, HTTPException

from forge.agent import AgentService, AgentTaskRequest, AgentTaskResult
from forge.api.errors import ERROR_RESPONSES
from forge.api.routes.generation import require_generation_api_key
from forge.config import ForgeSettings, get_settings
from forge.context import WorkspaceError
from forge.providers import get_provider

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/tasks", response_model=AgentTaskResult, responses=ERROR_RESPONSES)
async def run_task(
    payload: AgentTaskRequest,
    settings: ForgeSettings = Depends(get_settings),
    _: None = Depends(require_generation_api_key),
) -> AgentTaskResult:
    if settings.default_model not in {"ollama", "gemini", "openrouter"}:
        raise HTTPException(status_code=501)
    if settings.agent_workspace_root is None:
        raise HTTPException(status_code=503)

    try:
        service = AgentService(
            settings.agent_workspace_root,
            get_provider(settings.default_model, settings),
        )
        return await service.run(payload)
    except WorkspaceError as exc:
        raise HTTPException(status_code=422, detail="Workspace context could not be read.") from exc
