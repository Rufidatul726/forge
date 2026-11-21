from fastapi import APIRouter, Depends, HTTPException

from forge.agent import AgentService, AgentTaskRequest, AgentTaskResult
from forge.api.errors import ERROR_RESPONSES
from forge.api.routes.generation import require_generation_api_key
from forge.config import ForgeSettings, get_settings
from forge.context import WorkspaceError
from forge.providers import get_provider
from forge.providers.models import ChatMessage
from forge.sessions import SessionNotFoundError, SessionStore

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post(
    "/tasks",
    response_model=AgentTaskResult,
    response_model_exclude_none=True,
    responses=ERROR_RESPONSES,
)
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
        store = SessionStore(settings.agent_workspace_root) if payload.session_id else None
        if store is not None:
            saved_session = store.get(payload.session_id)
            history = [ChatMessage(role=turn.role, content=turn.content) for turn in saved_session.turns]
            if len(history) + len(payload.messages) > 32:
                raise HTTPException(status_code=422)
            payload = payload.model_copy(update={"messages": [*history, *payload.messages]})
        service = AgentService(
            settings.agent_workspace_root,
            get_provider(settings.default_model, settings),
        )
        result = await service.run(payload)
        if store is not None:
            new_messages = payload.messages[len(history) :]
            store.append_turns(payload.session_id, new_messages)
            store.append_turns(
                payload.session_id,
                [ChatMessage(role="assistant", content=result.generation.content)],
            )
            store.append_outcome(
                payload.session_id,
                "generation",
                {"model": result.generation.model, "done": result.generation.done},
            )
            return result.model_copy(update={"session_id": payload.session_id})
        return result
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    except WorkspaceError as exc:
        raise HTTPException(status_code=422, detail="Workspace context could not be read.") from exc
