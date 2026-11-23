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

logger = logging.getLogger("forge.research.providers.searxng")


class SearXNGSearchProvider:
    """Search adapter for SearXNG instances."""

    name = "searxng"

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.strip().rstrip("/")
        self._endpoint = f"{self._base_url}/search"
        self._timeout_seconds = timeout_seconds
        self._custom_client = client

    def _build_query(self, query: ResearchQuery) -> str:
        q = query.query.strip()
        if query.domain_restrictions:
            site_terms = " OR ".join(f"site:{d}" for d in query.domain_restrictions)
            q = f"{q} ({site_terms})"
        return q

    async def search(self, query: ResearchQuery) -> list[SearchResult]:
        q_text = self._build_query(query)
        params: dict[str, str | int] = {
            "q": q_text,
            "format": "json",
        }
        if query.recency:
            time_range_map = {"day": "day", "week": "week", "month": "month", "year": "year"}
            if query.recency.lower() in time_range_map:
                params["time_range"] = time_range_map[query.recency.lower()]

        client = self._custom_client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
        )
        try:
            response = await client.get(self._endpoint, params=params)
            if response.status_code == 429:
                raise SearchRateLimitError("searxng")
            if response.status_code != 200:
                raise SearchError(
                    f"SearXNG returned HTTP {response.status_code}: {response.text}",
                    public_message=f"SearXNG returned error status {response.status_code}.",
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
            raise SearchTimeoutError("searxng") from exc
        except httpx.HTTPError as exc:
            raise SearchError(f"Network error querying SearXNG: {exc}", public_message="Could not reach search provider") from exc
        finally:
            if self._custom_client is None:
                await client.aclose()
