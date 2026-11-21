import asyncio

import httpx
import pytest
from fastapi import FastAPI

from forge.api.main import create_app
from forge.config import ForgeSettings
from forge.providers.models import ChatMessage, GenerationResult
from forge.sessions import SessionNotFoundError, SessionStore


def request(app: FastAPI, method: str, path: str, *, headers=None, json_body=None) -> httpx.Response:
    async def send_request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, headers=headers, json=json_body)

    return asyncio.run(send_request())


def test_session_store_persists_turns_outcomes_and_safe_deletion(tmp_path):
    store = SessionStore(tmp_path)
    created = store.create("Repository review")
    store.append_turns(created.id, [ChatMessage(role="user", content="Review this repository")])
    store.append_outcome(created.id, "generation", {"model": "test", "done": True})

    restored = SessionStore(tmp_path).get(created.id)

    assert restored.summary.title == "Repository review"
    assert restored.summary.turn_count == 1
    assert restored.turns[0].content == "Review this repository"
    assert restored.outcomes[0].payload == {"model": "test", "done": True}
    assert (tmp_path / ".forge" / "sessions.sqlite3").exists()

    store.delete(created.id)
    with pytest.raises(SessionNotFoundError):
        store.get(created.id)


def test_session_api_and_agent_resume_share_persisted_history(monkeypatch, tmp_path):
    captured_messages = []

    class FakeProvider:
        async def generate(self, payload):
            captured_messages.extend(payload.messages)
            return GenerationResult(model=payload.model, content="Second response", done=True)

    monkeypatch.setattr("forge.api.routes.agent.get_provider", lambda name, settings: FakeProvider())
    api_key = "a" * 40
    app = create_app(
        ForgeSettings(
            _env_file=None,
            forge_api_key=api_key,
            agent_workspace_root=tmp_path,
        )
    )
    headers = {"Authorization": f"Bearer {api_key}"}
    created = request(
        app,
        "POST",
        "/api/v1/sessions",
        headers=headers,
        json_body={"title": "Chat"},
    )
    session_id = created.json()["id"]

    first = request(
        app,
        "POST",
        "/api/v1/agent/tasks",
        headers=headers,
        json_body={
            "model": "qwen2.5-coder:7b",
            "messages": [{"role": "user", "content": "First question"}],
            "session_id": session_id,
        },
    )
    second = request(
        app,
        "POST",
        "/api/v1/agent/tasks",
        headers=headers,
        json_body={
            "model": "qwen2.5-coder:7b",
            "messages": [{"role": "user", "content": "Second question"}],
            "session_id": session_id,
        },
    )
    restored = request(app, "GET", f"/api/v1/sessions/{session_id}", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["session_id"] == session_id
    assert [message.content for message in captured_messages if message.role != "system"] == [
        "First question",
        "First question",
        "Second response",
        "Second question",
    ]
    assert [turn["content"] for turn in restored.json()["turns"]] == [
        "First question",
        "Second response",
        "Second question",
        "Second response",
    ]
    assert len(restored.json()["outcomes"]) == 2


def test_session_api_requires_authentication_and_exact_existing_id(tmp_path):
    api_key = "a" * 40
    app = create_app(
        ForgeSettings(
            _env_file=None,
            forge_api_key=api_key,
            agent_workspace_root=tmp_path,
        )
    )

    unauthorized = request(app, "GET", "/api/v1/sessions")
    missing = request(
        app,
        "DELETE",
        f"/api/v1/sessions/{'a' * 32}",
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert unauthorized.status_code == 401
    assert missing.status_code == 404
