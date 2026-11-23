import httpx
import pytest
from pydantic import SecretStr

from forge.config import ForgeSettings
from forge.research.models import ResearchQuery
from forge.research.providers import (
    BraveSearchProvider,
    MockSearchProvider,
    SearXNGSearchProvider,
    TavilySearchProvider,
    UnconfiguredSearchProvider,
    get_search_provider,
)
from forge.research.providers.base import (
    SearchError,
    SearchProviderNotConfiguredError,
    SearchRateLimitError,
    SearchTimeoutError,
)


@pytest.mark.anyio
async def test_brave_search_provider_normalization():
    response_payload = {
        "web": {
            "results": [
                {
                    "url": "https://fastapi.tiangolo.com/tutorial/",
                    "title": "Tutorial - User Guide - FastAPI",
                    "description": "Learn FastAPI step by step.",
                },
                {
                    "url": "https://docs.pydantic.dev/",
                    "title": "Pydantic Documentation",
                    "description": "Data validation using Python type hints.",
                },
            ]
        }
    }
    mock_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=response_payload)
    )
    client = httpx.AsyncClient(transport=mock_transport)
    provider = BraveSearchProvider(api_key="test-brave-key", client=client)

    query = ResearchQuery(query="FastAPI tutorial", limit=5)
    results = await provider.search(query)

    assert len(results) == 2
    assert results[0].url == "https://fastapi.tiangolo.com/tutorial/"
    assert results[0].title == "Tutorial - User Guide - FastAPI"
    assert results[0].domain == "fastapi.tiangolo.com"
    assert results[0].rank == 1
    assert results[1].domain == "docs.pydantic.dev"


@pytest.mark.anyio
async def test_brave_search_provider_errors():
    # 429 Rate limit
    rate_limit_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(429, text="Rate limit"))
    )
    provider = BraveSearchProvider(api_key="test-key", client=rate_limit_client)
    with pytest.raises(SearchRateLimitError):
        await provider.search(ResearchQuery(query="test"))

    # 401 Auth error
    auth_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(401, text="Unauthorized"))
    )
    provider_auth = BraveSearchProvider(api_key="test-key", client=auth_client)
    with pytest.raises(SearchError, match="Brave Search API authentication failed"):
        await provider_auth.search(ResearchQuery(query="test"))


@pytest.mark.anyio
async def test_tavily_search_provider_normalization():
    payload = {
        "results": [
            {
                "url": "https://react.dev/learn",
                "title": "Quick Start - React",
                "content": "Welcome to the React documentation!",
            }
        ]
    }
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    )
    provider = TavilySearchProvider(api_key="test-tavily-key", client=client)
    results = await provider.search(ResearchQuery(query="React quick start", limit=3))

    assert len(results) == 1
    assert results[0].domain == "react.dev"
    assert "React" in results[0].title


@pytest.mark.anyio
async def test_searxng_search_provider_normalization():
    payload = {
        "results": [
            {
                "url": "https://docs.python.org/3/library/asyncio.html",
                "title": "asyncio — Asynchronous I/O — Python documentation",
                "content": "asyncio is a library to write concurrent code.",
            }
        ]
    }
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    )
    provider = SearXNGSearchProvider(base_url="https://searx.example.org", client=client)
    results = await provider.search(ResearchQuery(query="python asyncio", limit=2))

    assert len(results) == 1
    assert results[0].domain == "docs.python.org"


@pytest.mark.anyio
async def test_mock_search_provider():
    provider = MockSearchProvider()
    query = ResearchQuery(query="dependency injection", limit=3, domain_restrictions=["fastapi.tiangolo.com"])
    results = await provider.search(query)

    assert len(results) == 3
    assert results[0].domain == "fastapi.tiangolo.com"
    assert "dependency injection" in results[0].title


@pytest.mark.anyio
async def test_unconfigured_search_provider_raises():
    provider = UnconfiguredSearchProvider()
    with pytest.raises(SearchProviderNotConfiguredError, match="No web search provider configured"):
        await provider.search(ResearchQuery(query="test"))


def test_search_provider_factory():
    # Mock settings
    s_mock = ForgeSettings(search_provider="mock")
    assert isinstance(get_search_provider(s_mock), MockSearchProvider)

    # Brave settings
    s_brave = ForgeSettings(brave_search_api_key=SecretStr("brave-token"))
    assert isinstance(get_search_provider(s_brave), BraveSearchProvider)

    # Tavily settings
    s_tavily = ForgeSettings(tavily_api_key=SecretStr("tavily-token"))
    assert isinstance(get_search_provider(s_tavily), TavilySearchProvider)

    # SearXNG settings
    s_searx = ForgeSettings(searxng_url="https://searx.internal")
    assert isinstance(get_search_provider(s_searx), SearXNGSearchProvider)

    # Unconfigured settings
    s_unconfigured = ForgeSettings()
    assert isinstance(get_search_provider(s_unconfigured), UnconfiguredSearchProvider)
