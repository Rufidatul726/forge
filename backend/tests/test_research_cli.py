from pathlib import Path
from typer.testing import CliRunner

from forge.cli import app
from forge.research.models import (
    ResearchQuery,
    ResearchReport,
    ResearchSource,
    ResearchStatus,
    RetrievedDocument,
)


def test_research_cli_help():
    runner = CliRunner()
    result = runner.invoke(app, ["research", "--help"])
    assert result.exit_code == 0
    assert "Web research, documentation retrieval" in result.stdout
    assert "fetch" in result.stdout
    assert "cache" in result.stdout


def test_research_cli_query_with_mock_service(tmp_path: Path, monkeypatch):
    runner = CliRunner()

    class FakeResearchService:
        def __init__(self, root):
            self.workspace_root = root

        async def research(self, query: ResearchQuery):
            src = ResearchSource(
                source_id="src_1",
                canonical_url="https://docs.pydantic.dev/latest/",
                original_url="https://docs.pydantic.dev/latest/",
                page_title="Pydantic Overview",
                domain="docs.pydantic.dev",
                retrieval_timestamp="2026-01-01T00:00:00Z",
                content_type="text/html",
                extracted_text="Pydantic is a data validation library.",
                content_hash="h1",
                retrieval_status="success",
                relevance_score=80.0,
            )
            return ResearchReport(
                research_id="rep_cli_test",
                query=query,
                sources=[src],
                verified_facts=["Pydantic is a data validation library."],
                status=ResearchStatus.COMPLETED,
            )

    monkeypatch.setattr("forge.cli.ResearchService", FakeResearchService)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["research", "Pydantic v2 validation", "--limit", "3"])
    assert result.exit_code == 0
    assert "Retrieved & Ranked Sources" in result.stdout
    assert "Pydantic Overview" in result.stdout
    assert "rep_cli_test" in result.stdout


def test_research_cli_fetch(tmp_path: Path, monkeypatch):
    runner = CliRunner()

    class FakeResearchService:
        def __init__(self, root):
            pass

        async def fetch_url(self, url: str):
            return RetrievedDocument(
                canonical_url=url,
                original_url=url,
                title="Mock Fetch Doc",
                domain="example.com",
                content_type="text/html",
                extracted_text="Fetched body content for testing.",
                content_hash="abc",
            )

    monkeypatch.setattr("forge.cli.ResearchService", FakeResearchService)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["research", "fetch", "https://example.com/docs"])
    assert result.exit_code == 0
    assert "Mock Fetch Doc" in result.stdout
    assert "Fetched body content" in result.stdout


def test_research_cli_cache_commands(tmp_path: Path, monkeypatch):
    runner = CliRunner()

    class FakeCache:
        def status(self):
            return {
                "total_documents": 5,
                "total_size_mb": 1.25,
                "max_documents_limit": 500,
                "max_bytes_limit_mb": 50.0,
                "ttl_days": 7,
                "expired_documents": 0,
                "oldest_entry": "2026-01-01",
                "newest_entry": "2026-01-02",
            }

        def clear(self):
            return 5

    class FakeResearchService:
        def __init__(self, root):
            self.cache = FakeCache()

    monkeypatch.setattr("forge.cli.ResearchService", FakeResearchService)
    monkeypatch.chdir(tmp_path)

    # status
    res_status = runner.invoke(app, ["research", "cache", "--status"])
    assert res_status.exit_code == 0
    assert "Research Cache Status" in res_status.stdout
    assert "1.25 MB" in res_status.stdout

    # clear
    res_clear = runner.invoke(app, ["research", "cache", "--clear"])
    assert res_clear.exit_code == 0
    assert "Cleared 5 document(s)" in res_clear.stdout
