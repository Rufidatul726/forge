import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

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


@router.post(
    "/tasks/stream",
    responses=ERROR_RESPONSES,
)
async def stream_task(
    payload: AgentTaskRequest,
    settings: ForgeSettings = Depends(get_settings),
    _: None = Depends(require_generation_api_key),
) -> StreamingResponse:
    if settings.default_model not in {"ollama", "gemini", "openrouter"}:
        raise HTTPException(status_code=501)
    if settings.agent_workspace_root is None:
        raise HTTPException(status_code=503)

    store: SessionStore | None = None
    history: list[ChatMessage] = []
    if payload.session_id:
        store = SessionStore(settings.agent_workspace_root)
        try:
            saved_session = store.get(payload.session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404) from exc
        history = [ChatMessage(role=turn.role, content=turn.content) for turn in saved_session.turns]
        if len(history) + len(payload.messages) > 32:
            raise HTTPException(status_code=422)
        payload = payload.model_copy(update={"messages": [*history, *payload.messages]})

    service = AgentService(
        settings.agent_workspace_root,
        get_provider(settings.default_model, settings),
    )
    try:
        built_context = service._build_context(payload.context_paths)
    except WorkspaceError as exc:
        raise HTTPException(status_code=422, detail="Workspace context could not be read.") from exc

    async def event_generator() -> AsyncIterator[str]:
        full_content: list[str] = []
        last_model = payload.model
        try:
            async for chunk in service.stream(payload, context=built_context):
                if chunk.model:
                    last_model = chunk.model
                if chunk.content:
                    full_content.append(chunk.content)
                    data = json.dumps({"content": chunk.content, "done": False, "model": last_model})
                    yield f"data: {data}\n\n"
                if chunk.done:
                    break

            completed_text = "".join(full_content)
            if store is not None and payload.session_id is not None:
                new_messages = payload.messages[len(history):]
                store.append_turns(payload.session_id, new_messages)
                store.append_turns(
                    payload.session_id,
                    [ChatMessage(role="assistant", content=completed_text)],
                )
                store.append_outcome(
                    payload.session_id,
                    "generation",
                    {"model": last_model, "done": True},
                )
            final_data = json.dumps({
                "content": "",
                "done": True,
                "model": last_model,
                "session_id": payload.session_id,
            })
            yield f"data: {final_data}\n\n"
        except WorkspaceError:
            err = json.dumps({"error": {"code": "workspace_error", "message": "Workspace context could not be read."}})
            yield f"event: error\ndata: {err}\n\n"
        except Exception as exc:
            code = getattr(exc, "code", "provider_error")
            msg = getattr(exc, "public_message", "Streaming task execution failed.")
            err = json.dumps({"error": {"code": str(code), "message": str(msg)}})
            yield f"event: error\ndata: {err}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
