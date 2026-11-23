from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from forge.api.main import create_app
from forge.config import ForgeSettings
from forge.research.models import ResearchQuery, ResearchReport, ResearchSource, ResearchStatus


def test_research_api_endpoints(tmp_path: Path, monkeypatch):
    settings = ForgeSettings(
        environment="development",
        agent_workspace_root=tmp_path,
        forge_api_key="a" * 32,
    )
    app = create_app(settings)
    client = TestClient(app)

    headers = {"Authorization": f"Bearer {'a' * 32}"}

    class FakeResearchService:
        def __init__(self, root, settings=None):
            pass

        async def research(self, query: ResearchQuery):
            src = ResearchSource(
                source_id="src_1",
                canonical_url="https://docs.pydantic.dev/",
                original_url="https://docs.pydantic.dev/",
                page_title="Pydantic",
                domain="docs.pydantic.dev",
                retrieval_timestamp="2026-01-01T00:00:00Z",
                content_type="text/html",
                extracted_text="Pydantic library",
                content_hash="h1",
                relevance_score=90.0,
            )
            return ResearchReport(
                research_id="rep_api_123",
                query=query,
                sources=[src],
                status=ResearchStatus.COMPLETED,
            )

        def get_report(self, report_id: str):
            if report_id == "rep_api_123":
                src = ResearchSource(
                    source_id="src_1",
                    canonical_url="https://docs.pydantic.dev/",
                    original_url="https://docs.pydantic.dev/",
                    page_title="Pydantic",
                    domain="docs.pydantic.dev",
                    retrieval_timestamp="2026-01-01T00:00:00Z",
                    content_type="text/html",
                    extracted_text="Pydantic library",
                    content_hash="h1",
                    relevance_score=90.0,
                )
                return ResearchReport(
                    research_id="rep_api_123",
                    query=ResearchQuery(query="Pydantic"),
                    sources=[src],
                    status=ResearchStatus.COMPLETED,
                )
            return None

    monkeypatch.setattr("forge.api.routes.research.ResearchService", FakeResearchService)

    # 1. Test POST /api/v1/research requires auth
    res_unauth = client.post("/api/v1/research", json={"query": "Pydantic"})
    assert res_unauth.status_code == 401

    # 2. Test POST /api/v1/research with auth
    res_create = client.post("/api/v1/research", json={"query": "Pydantic"}, headers=headers)
    assert res_create.status_code == 200
    data = res_create.json()
    assert data["research_id"] == "rep_api_123"
    assert len(data["sources"]) == 1

    # 3. Test GET /api/v1/research/{id}
    res_get = client.get("/api/v1/research/rep_api_123", headers=headers)
    assert res_get.status_code == 200
    assert res_get.json()["research_id"] == "rep_api_123"

    # 4. Test GET /api/v1/research/{id}/sources
    res_sources = client.get("/api/v1/research/rep_api_123/sources", headers=headers)
    assert res_sources.status_code == 200
    assert len(res_sources.json()) == 1
    assert res_sources.json()[0]["source_id"] == "src_1"

    # 5. Test 404 for nonexistent research report
    res_404 = client.get("/api/v1/research/rep_nonexistent", headers=headers)
    assert res_404.status_code == 404
