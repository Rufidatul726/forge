from __future__ import annotations

from typing import Protocol, runtime_checkable

from forge.research.models import ResearchQuery, RetrievedDocument, SearchResult


@runtime_checkable
class SearchProvider(Protocol):
    """Protocol for web and documentation search adapters."""

    name: str

    async def search(self, query: ResearchQuery) -> list[SearchResult]:
        """Execute a search query and return normalized results."""
        ...


@runtime_checkable
class DocumentFetcher(Protocol):
    """Protocol for secure, bounded HTTP document retrieval."""

    async def fetch(self, url: str) -> RetrievedDocument:
        """Fetch and extract content from a URL safely."""
        ...
