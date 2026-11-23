import asyncio
from pathlib import Path
import pytest

from forge.agent import (
    AgentService,
    AgentTaskRequest,
    ExecutionPolicy,
    create_default_registry,
)
from forge.agent.dispatcher import ToolDispatcher
from forge.agent.policy import ExecutionPolicyConfig
from forge.agent.registry import ToolExecutionContext
from forge.context import Workspace
from forge.execution import CommandRunner
from forge.providers.models import (
    ChatMessage,
    GenerationResult,
    ToolCall,
)
from forge.research.models import ResearchQuery, RetrievedDocument, SearchResult
from forge.research.providers import MockSearchProvider
from forge.research.service import ResearchService


def run(coro):
    return asyncio.run(coro)


def test_research_tools_registered_as_read_only():
    registry = create_default_registry()
    tools = registry.list_tools()

    assert "web_search" in tools
    assert "fetch_url" in tools
    assert "search_documentation" in tools
    assert "inspect_research" in tools

    assert registry.get("web_search").read_only is True
    assert registry.get("fetch_url").read_only is True
    assert registry.get("search_documentation").read_only is True
    assert registry.get("inspect_research").read_only is True


def test_dispatcher_handles_fetch_url(tmp_path: Path):
    registry = create_default_registry()
    workspace = Workspace(tmp_path)
    policy_config = ExecutionPolicyConfig(policy=ExecutionPolicy.PLAN)

    class FakeService:
        async def fetch_url(self, url: str):
            return RetrievedDocument(
                canonical_url=url,
                original_url=url,
                title="Mocked Doc",
                domain="example.com",
                content_type="text/html",
                extracted_text="Safe extracted content from documentation.",
                content_hash="h1",
            )

    ctx = ToolExecutionContext(
        workspace=workspace,
        command_runner=CommandRunner(tmp_path),
        policy_config=policy_config,
        approve_write=lambda p: False,
        approve_command=lambda p: False,
        research_service=FakeService(),
    )
    dispatcher = ToolDispatcher(registry, ctx)

    call = ToolCall(name="fetch_url", arguments={"url": "https://example.com/api"})
    res = run(dispatcher.dispatch(call))

    assert res.ok is True
    assert "Mocked Doc" in res.content
    assert "Safe extracted content" in res.content


def test_agent_autonomous_loop_uses_search_documentation(tmp_path: Path):
    mock_search = MockSearchProvider(
        canned_results=[
            SearchResult(
                url="https://docs.pydantic.dev/2.0/migration/",
                title="Pydantic V2 Migration Guide",
                snippet="Use field_validator instead of validator.",
                rank=1,
                domain="docs.pydantic.dev",
            )
        ]
    )
    research_service = ResearchService(tmp_path, search_provider=mock_search)

    class FakeModelProvider:
        def __init__(self):
            self.calls = 0

        async def generate(self, payload):
            self.calls += 1
            if self.calls == 1:
                return GenerationResult(
                    model=payload.model,
                    tool_calls=[
                        ToolCall(
                            name="search_documentation",
                            arguments={"query": "Pydantic v2 validator migration"},
                        )
                    ],
                    done=True,
                )
            return GenerationResult(
                model=payload.model,
                content="Based on [S1] (docs.pydantic.dev), replace @validator with @field_validator.",
                done=True,
            )

    provider = FakeModelProvider()
    service = AgentService(tmp_path, provider, research_service=research_service)

    result = run(
        service.run_autonomous(
            AgentTaskRequest(
                model="test-model",
                messages=[ChatMessage(role="user", content="How do I migrate validators in Pydantic v2?")],
            ),
            approve_write=lambda proposal: False,
            approve_command=lambda proposal: False,
            policy=ExecutionPolicy.PLAN,  # Should work in PLAN mode because research is read-only!
        )
    )

    assert result.generation.content is not None
    assert "[S1]" in result.generation.content
    assert len(result.tool_outcomes) == 1
    assert result.tool_outcomes[0].name == "search_documentation"
    assert result.tool_outcomes[0].ok is True
