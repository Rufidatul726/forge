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

logger = logging.getLogger("forge.research.providers.brave")

BRAVE_SEARCH_API_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveSearchProvider:
    """Search adapter for the official Brave Search API."""

    name = "brave"

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = BRAVE_SEARCH_API_ENDPOINT,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds
        self._custom_client = client

    def _build_query(self, query: ResearchQuery) -> str:
        q = query.query.strip()
        if query.domain_restrictions:
            if len(query.domain_restrictions) == 1:
                q = f"{q} site:{query.domain_restrictions[0]}"
            else:
                site_terms = " OR ".join(f"site:{d}" for d in query.domain_restrictions)
                q = f"{q} ({site_terms})"
        return q

    async def search(self, query: ResearchQuery) -> list[SearchResult]:
        q_text = self._build_query(query)
        params: dict[str, str | int] = {
            "q": q_text,
            "count": max(1, min(query.limit, 20)),
            "text_decorations": "false",
        }
        if query.recency:
            # Map recency if specified
            recency_map = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
            params["freshness"] = recency_map.get(query.recency.lower(), query.recency)

        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
            "User-Agent": "Forge-Research/0.1",
        }

        client = self._custom_client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            headers=headers,
        )
        try:
            response = await client.get(self._endpoint, params=params)
            if response.status_code == 429:
                raise SearchRateLimitError("brave")
            if response.status_code == 401 or response.status_code == 403:
                raise SearchError(
                    f"Brave Search API authentication failed (HTTP {response.status_code}). Check BRAVE_SEARCH_API_KEY.",
                    public_message="Brave Search authentication failed. Please verify your API key.",
                )
            if response.status_code != 200:
                raise SearchError(
                    f"Brave Search API returned HTTP error {response.status_code}: {response.text}",
                    public_message=f"Brave Search returned error status {response.status_code}.",
                )

            data = response.json()
            web_results = data.get("web", {}).get("results", [])
            normalized: list[SearchResult] = []

            for rank, item in enumerate(web_results[:query.limit], start=1):
                url = item.get("url", "").strip()
                title = item.get("title", "").strip()
                snippet = item.get("description", "").strip()
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
            raise SearchTimeoutError("brave") from exc
        except httpx.HTTPError as exc:
            raise SearchError(f"Network error querying Brave Search: {exc}", public_message="Could not reach search provider") from exc
        finally:
            if self._custom_client is None:
                await client.aclose()
