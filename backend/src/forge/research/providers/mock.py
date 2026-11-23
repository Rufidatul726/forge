from __future__ import annotations

from forge.research.models import ResearchQuery, SearchResult
from forge.research.providers.base import SearchProviderNotConfiguredError


class MockSearchProvider:
    """Mock search provider for testing without external networks."""

    name = "mock"

    def __init__(self, canned_results: list[SearchResult] | None = None) -> None:
        self.canned_results = canned_results or []
        self.recorded_queries: list[ResearchQuery] = []

    async def search(self, query: ResearchQuery) -> list[SearchResult]:
        self.recorded_queries.append(query)
        if self.canned_results:
            return self.canned_results[:query.limit]

        # Generate deterministic mock results matching query
        results: list[SearchResult] = []
        for i in range(1, query.limit + 1):
            domain = query.domain_restrictions[0] if query.domain_restrictions else "docs.example.org"
            results.append(
                SearchResult(
                    url=f"https://{domain}/doc/page{i}",
                    title=f"Documentation Page {i} for {query.query}",
                    snippet=f"Relevant information about {query.query} in section {i}.",
                    rank=i,
                    domain=domain,
                    source_type="documentation",
                )
            )
        return results


class UnconfiguredSearchProvider:
    """Placeholder search provider when no search API key/instance is configured."""

    name = "unconfigured"

    async def search(self, query: ResearchQuery) -> list[SearchResult]:
        raise SearchProviderNotConfiguredError()
