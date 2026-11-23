from __future__ import annotations

from typing import Protocol

from forge.research.models import ResearchQuery, SearchResult
from forge.research.protocols import SearchProvider


class SearchError(Exception):
    """Base error for search operations."""

    def __init__(self, message: str, public_message: str | None = None) -> None:
        super().__init__(message)
        self.public_message = public_message or message


class SearchProviderNotConfiguredError(SearchError):
    """Raised when web search is invoked without configured search provider API credentials."""

    def __init__(
        self,
        message: str = (
            "No web search provider configured. Set BRAVE_SEARCH_API_KEY, TAVILY_API_KEY, "
            "or SEARXNG_URL in your environment or .env.local to enable external web search. "
            "Direct documentation retrieval by URL (e.g. fetch_url) remains available."
        ),
    ) -> None:
        super().__init__(message, public_message=message)


class SearchTimeoutError(SearchError):
    """Raised when search provider API request times out."""

    def __init__(self, provider: str) -> None:
        super().__init__(
            f"Search request to {provider} timed out",
            public_message=f"Search service ({provider}) timed out. Please try again.",
        )


class SearchRateLimitError(SearchError):
    """Raised when search provider API rate limit is exceeded."""

    def __init__(self, provider: str) -> None:
        super().__init__(
            f"Rate limit exceeded for {provider}",
            public_message=f"Search rate limit exceeded for {provider}. Please try again later.",
        )
