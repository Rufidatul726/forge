from __future__ import annotations

import logging

from forge.config import ForgeSettings
from forge.research.protocols import SearchProvider
from forge.research.providers.base import (
    SearchError,
    SearchProviderNotConfiguredError,
    SearchRateLimitError,
    SearchTimeoutError,
)
from forge.research.providers.brave import BraveSearchProvider
from forge.research.providers.mock import MockSearchProvider, UnconfiguredSearchProvider
from forge.research.providers.searxng import SearXNGSearchProvider
from forge.research.providers.tavily import TavilySearchProvider

logger = logging.getLogger("forge.research.providers")


def get_search_provider(settings: ForgeSettings) -> SearchProvider:
    """Instantiate configured search provider adapter based on settings and environment."""
    provider_choice = settings.search_provider.lower()

    if provider_choice == "mock":
        return MockSearchProvider()

    if provider_choice == "brave" or (
        provider_choice == "auto" and settings.brave_search_api_key is not None
    ):
        if settings.brave_search_api_key is not None:
            return BraveSearchProvider(
                api_key=settings.brave_search_api_key.get_secret_value(),
            )
        raise SearchProviderNotConfiguredError("Search provider is set to 'brave' but BRAVE_SEARCH_API_KEY is not configured.")

    if provider_choice == "tavily" or (
        provider_choice == "auto" and settings.tavily_api_key is not None
    ):
        if settings.tavily_api_key is not None:
            return TavilySearchProvider(
                api_key=settings.tavily_api_key.get_secret_value(),
            )
        raise SearchProviderNotConfiguredError("Search provider is set to 'tavily' but TAVILY_API_KEY is not configured.")

    if provider_choice == "searxng" or (
        provider_choice == "auto" and settings.searxng_url is not None
    ):
        if settings.searxng_url is not None:
            return SearXNGSearchProvider(base_url=settings.searxng_url)
        raise SearchProviderNotConfiguredError("Search provider is set to 'searxng' but SEARXNG_URL is not configured.")

    return UnconfiguredSearchProvider()
