from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from forge.api.errors import ERROR_RESPONSES
from forge.api.routes.generation import require_generation_api_key
from forge.config import ForgeSettings, get_settings
from forge.sessions import Session, SessionNotFoundError, SessionStore, SessionSummary

router = APIRouter(prefix="/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    title: str = Field(default="New session", min_length=1, max_length=120)


class SessionSummaryResponse(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    turn_count: int


class SessionTurnResponse(BaseModel):
    role: str
    content: str
    created_at: str


class SessionOutcomeResponse(BaseModel):
    kind: str
    payload: dict[str, object]
    created_at: str


class SessionResponse(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    turn_count: int
    turns: list[SessionTurnResponse]
    outcomes: list[SessionOutcomeResponse]


SessionId = Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]+$")]


def get_session_store(
    _: None = Depends(require_generation_api_key),
    settings: ForgeSettings = Depends(get_settings),
) -> SessionStore:
    if settings.agent_workspace_root is None:
        raise HTTPException(status_code=503)
    return SessionStore(settings.agent_workspace_root)


@router.post("", response_model=SessionSummaryResponse, responses=ERROR_RESPONSES)
def create_session(
    payload: CreateSessionRequest,
    store: SessionStore = Depends(get_session_store),
) -> SessionSummary:
    return store.create(payload.title)


@router.get("", response_model=list[SessionSummaryResponse], responses=ERROR_RESPONSES)
def list_sessions(
    limit: int = 100,
    store: SessionStore = Depends(get_session_store),
) -> list[SessionSummary]:
    return store.list(limit=limit)


@router.get("/{session_id}", response_model=SessionResponse, responses=ERROR_RESPONSES)
def get_session(
    session_id: SessionId,
    store: SessionStore = Depends(get_session_store),
) -> SessionResponse:
    try:
        return _session_response(store.get(session_id))
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404) from exc


@router.delete("/{session_id}", status_code=204, responses=ERROR_RESPONSES)
def delete_session(
    session_id: SessionId,
    store: SessionStore = Depends(get_session_store),
) -> Response:
    try:
        store.delete(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    return Response(status_code=204)


def _session_response(session: Session) -> SessionResponse:
    return SessionResponse(
        **session.summary.__dict__,
        turns=[SessionTurnResponse(**turn.__dict__) for turn in session.turns],
        outcomes=[SessionOutcomeResponse(**outcome.__dict__) for outcome in session.outcomes],
    )
