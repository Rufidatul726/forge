from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx

from forge.research.models import ResearchQuery, SearchResult
from forge.research.providers.base import (
    SearchError,
    SearchRateLimitError,
    SearchTimeoutError,
)

logger = logging.getLogger("forge.research.providers.tavily")

TAVILY_SEARCH_API_ENDPOINT = "https://api.tavily.com/search"


class TavilySearchProvider:
    """Search adapter for Tavily Search API."""

    name = "tavily"

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = TAVILY_SEARCH_API_ENDPOINT,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds
        self._custom_client = client

    async def search(self, query: ResearchQuery) -> list[SearchResult]:
        payload: dict[str, object] = {
            "api_key": self._api_key,
            "query": query.query.strip(),
            "max_results": max(1, min(query.limit, 20)),
            "search_depth": "basic",
            "include_answer": False,
        }
        if query.domain_restrictions:
            payload["include_domains"] = query.domain_restrictions

        client = self._custom_client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
        )
        try:
            response = await client.post(self._endpoint, json=payload)
            if response.status_code == 429:
                raise SearchRateLimitError("tavily")
            if response.status_code in {401, 403}:
                raise SearchError(
                    f"Tavily authentication failed (HTTP {response.status_code}). Check TAVILY_API_KEY.",
                    public_message="Tavily authentication failed. Please verify your API key.",
                )
            if response.status_code != 200:
                raise SearchError(
                    f"Tavily returned HTTP {response.status_code}: {response.text}",
                    public_message=f"Tavily search returned error status {response.status_code}.",
                )

            data = response.json()
            raw_results = data.get("results", [])
            normalized: list[SearchResult] = []

            for rank, item in enumerate(raw_results[:query.limit], start=1):
                url = item.get("url", "").strip()
                title = item.get("title", "").strip()
                snippet = item.get("content", "").strip()
                if not url or not title:
                    continue
                domain = urlsplit(url).netloc.lower()
                normalized.append(
                    SearchResult(
                        url=url,
                        title=title,
                        snippet=snippet,
                        rank=rank,
                        domain=domain,
                        source_type="web",
                    )
                )

            return normalized

        except httpx.TimeoutException as exc:
            raise SearchTimeoutError("tavily") from exc
        except httpx.HTTPError as exc:
            raise SearchError(f"Network error querying Tavily: {exc}", public_message="Could not reach search provider") from exc
        finally:
            if self._custom_client is None:
                await client.aclose()
