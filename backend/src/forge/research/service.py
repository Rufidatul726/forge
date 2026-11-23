from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from forge.config import ForgeSettings, get_settings
from forge.research.cache import ResearchCache
from forge.research.citations import (
    CitationValidator,
    extract_evidence_excerpts,
)
from forge.research.extraction import extract_content
from forge.research.fetcher import SafeDocumentFetcher
from forge.research.models import (
    Citation,
    EvidenceExcerpt,
    FindingKind,
    ResearchFinding,
    ResearchQuery,
    ResearchReport,
    ResearchSource,
    ResearchStatus,
    RetrievedDocument,
    SearchResult,
)
from forge.research.protocols import SearchProvider
from forge.research.providers import get_search_provider
from forge.research.ranking import rank_and_deduplicate_sources
from forge.research.storage import ResearchStore

logger = logging.getLogger("forge.research.service")


class ResearchService:
    """Unified coordinator for web search, secure document retrieval, caching, and evidence grounding."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        search_provider: SearchProvider | None = None,
        fetcher: SafeDocumentFetcher | None = None,
        store: ResearchStore | None = None,
        cache: ResearchCache | None = None,
        settings: ForgeSettings | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.settings = settings or get_settings()
        self.search_provider = search_provider or get_search_provider(self.settings)
        self.fetcher = fetcher or SafeDocumentFetcher()
        self.store = store or ResearchStore(self.workspace_root)
        self.cache = cache or ResearchCache(
            self.workspace_root,
            ttl_days=self.settings.research_cache_ttl_days,
            max_documents=self.settings.research_max_cache_documents,
            max_cache_bytes=self.settings.research_max_cache_bytes,
        )

    async def fetch_url(self, url: str) -> RetrievedDocument:
        """Retrieve and extract a single document, consulting the persistent cache first."""
        cached = self.cache.get(url)
        if cached is not None:
            return cached

        final_url, content_type, raw_text = await self.fetcher.fetch(url)
        extracted = extract_content(raw_text, content_type, base_url=final_url)

        domain = urlsplit(final_url).netloc.lower()
        doc = RetrievedDocument(
            canonical_url=extracted.canonical_url or final_url,
            original_url=url,
            title=extracted.title or domain,
            domain=domain,
            content_type=content_type,
            raw_content=None,
            extracted_text=extracted.text,
            headings=extracted.headings,
            code_blocks=extracted.code_blocks,
            content_hash=extracted.content_hash,
            retrieval_timestamp=datetime.now(UTC).isoformat(),
            status="success",
            extraction_warnings=extracted.warnings,
        )

        self.cache.put(doc)
        return doc

    async def research(self, query: ResearchQuery) -> ResearchReport:
        """Perform end-to-end web research, content retrieval, ranking, and evidence generation."""
        report = ResearchReport(query=query, status=ResearchStatus.RETRIEVING)

        # 1. Search external provider
        try:
            search_results = await self.search_provider.search(query)
        except Exception as exc:
            report.status = ResearchStatus.FAILED
            report.warnings.append(f"Search provider error: {exc}")
            self.store.save_report(report)
            raise

        if not search_results:
            report.status = ResearchStatus.COMPLETED
            report.summary = f"No search results found for query: {query.query!r}"
            self.store.save_report(report)
            return report

        # 2. Retrieve documents for search hits
        raw_sources: list[ResearchSource] = []
        for rank_idx, item in enumerate(search_results, start=1):
            source_id = f"src_{rank_idx}"
            try:
                doc = await self.fetch_url(item.url)
                raw_sources.append(
                    ResearchSource(
                        source_id=source_id,
                        canonical_url=doc.canonical_url,
                        original_url=doc.original_url,
                        page_title=doc.title or item.title,
                        domain=doc.domain or item.domain,
                        publication_or_modification_date=doc.publication_date or doc.modification_date,
                        retrieval_timestamp=doc.retrieval_timestamp,
                        content_type=doc.content_type,
                        extracted_text=doc.extracted_text,
                        content_hash=doc.content_hash,
                        retrieval_status="success",
                        search_rank=item.rank,
                        source_type=item.source_type,
                        extraction_warnings=doc.extraction_warnings,
                    )
                )
            except Exception as exc:
                logger.warning("Failed retrieving %s: %s", item.url, exc)
                report.warnings.append(f"Could not retrieve {item.url}: {exc}")
                # Keep source record with failed status and snippet
                raw_sources.append(
                    ResearchSource(
                        source_id=source_id,
                        canonical_url=item.url,
                        original_url=item.url,
                        page_title=item.title,
                        domain=item.domain,
                        retrieval_timestamp=datetime.now(UTC).isoformat(),
                        content_type="text/plain",
                        extracted_text=item.snippet,
                        content_hash="",
                        retrieval_status=f"failed: {exc}",
                        search_rank=item.rank,
                        source_type=item.source_type,
                        extraction_warnings=[str(exc)],
                    )
                )

        # 3. Deduplicate and Rank
        ranked_sources = rank_and_deduplicate_sources(raw_sources, query)
        # Re-index stable source IDs [src_1, src_2...] in ranked order
        for idx, src in enumerate(ranked_sources, start=1):
            src.source_id = f"src_{idx}"

        report.sources = ranked_sources

        # 4. Extract evidence excerpts and build citations
        all_excerpts: list[EvidenceExcerpt] = []
        all_citations: list[Citation] = []

        for src in ranked_sources:
            if src.retrieval_status != "success" or not src.extracted_text:
                continue

            excerpts = extract_evidence_excerpts(src, query, max_excerpts=3)
            all_excerpts.extend(excerpts)

            for exc in excerpts:
                all_citations.append(
                    Citation(
                        source_id=src.source_id,
                        source_title=src.page_title,
                        canonical_url=src.canonical_url,
                        domain=src.domain,
                        retrieval_date=src.retrieval_timestamp,
                        relevant_excerpt=exc.exact_text,
                        section_heading=exc.section_heading,
                        is_verified=True,
                    )
                )

        # Validate all citations against sources
        report.citations = CitationValidator.validate_report_citations(
            all_citations, ranked_sources
        )
        report.evidence_excerpts = all_excerpts

        # 5. Analyze findings and conflicts
        self._synthesize_report_findings(report)

        report.status = ResearchStatus.COMPLETED
        self.store.save_report(report)
        return report

    def _synthesize_report_findings(self, report: ResearchReport) -> None:
        """Derive structured verified facts, inferences, and conflicting sources."""
        query_text = report.query.query
        verified_facts: list[str] = []
        conflicts: list[dict[str, Any]] = []

        # Collect verified facts from top excerpts
        for cit in report.citations[:5]:
            heading_ctx = f" (in {cit.section_heading})" if cit.section_heading else ""
            fact = f"According to {cit.source_title} [{cit.source_id}]{heading_ctx}: {cit.relevant_excerpt[:200]}..."
            verified_facts.append(fact)

        report.verified_facts = verified_facts

        # Detect potential version conflicts among sources
        versions_seen: dict[str, list[str]] = {}
        for src in report.sources:
            for v_match in src.extracted_text[:1000].split():
                if v_match.startswith("v") and len(v_match) > 1 and v_match[1].isdigit():
                    versions_seen.setdefault(v_match, []).append(src.source_id)

        if len(versions_seen) > 1:
            conflicts.append({
                "issue": "Multiple versions referenced across retrieved documentation",
                "versions": {v: s_ids for v, s_ids in versions_seen.items() if len(s_ids) >= 1},
            })

        report.conflicting_sources = conflicts

        # Formulate recommendations & assumptions
        if report.sources:
            top_src = report.sources[0]
            report.recommendations.append(
                f"Refer to {top_src.page_title} ({top_src.canonical_url}) for primary implementation guidance."
            )
        else:
            report.unresolved_questions.append(
                f"No verified documentation could be retrieved for '{query_text}'."
            )

        report.summary = (
            f"Retrieved {len(report.sources)} sources with {len(report.evidence_excerpts)} evidence excerpts "
            f"for query: {query_text!r}."
        )

    def get_report(self, report_id: str) -> ResearchReport | None:
        """Load an existing report by ID."""
        return self.store.get_report(report_id)

    def list_reports(self, limit: int = 50) -> list[dict[str, Any]]:
        """List summary of recorded research reports."""
        return self.store.list_reports(limit=limit)
