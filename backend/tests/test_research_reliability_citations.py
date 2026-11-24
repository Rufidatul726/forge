import tempfile
from pathlib import Path
import pytest

from forge.research.citations import (
    CitationValidationError,
    CitationValidator,
    extract_evidence_excerpts,
)
from forge.research.models import (
    Citation,
    EvidenceExcerpt,
    ResearchQuery,
    ResearchReport,
    ResearchSource,
    ResearchStatus,
)
from forge.research.ranking import rank_and_deduplicate_sources
from forge.research.service import ResearchService
from forge.research.storage import ResearchStore


# ==============================================================================
# SECTION 5: Evidence & Citation Reliability Tests
# ==============================================================================

def make_source(source_id: str = "src_1", text: str = "Default content") -> ResearchSource:
    return ResearchSource(
        source_id=source_id,
        canonical_url="https://example.com/docs/intro",
        original_url="https://example.com/docs/intro",
        page_title="Introduction Docs",
        domain="example.com",
        retrieval_timestamp="2026-01-01T00:00:00Z",
        content_type="text/html",
        extracted_text=text,
        content_hash="hash_123",
        source_type="documentation",
    )


def test_citation_validator_valid_offsets():
    """Citation with accurate offsets matches the slice in source text."""
    text = "The quick brown fox jumps over the lazy dog."
    source = make_source("src_1", text)
    # "brown fox" starts at index 10, ends at 19
    start = text.index("brown fox")
    end = start + len("brown fox")

    citation = Citation(
        source_id="src_1",
        source_title=source.page_title,
        canonical_url=source.canonical_url,
        domain=source.domain,
        retrieval_date=source.retrieval_timestamp,
        relevant_excerpt="brown fox",
        start_offset=start,
        end_offset=end,
    )
    assert CitationValidator.validate_citation(citation, {"src_1": source}) is True


def test_citation_validator_rejects_incorrect_offsets():
    """Citation where start/end offsets point to a different slice is rejected."""
    text = "The quick brown fox jumps over the lazy dog."
    source = make_source("src_1", text)

    citation = Citation(
        source_id="src_1",
        source_title=source.page_title,
        canonical_url=source.canonical_url,
        domain=source.domain,
        retrieval_date=source.retrieval_timestamp,
        relevant_excerpt="brown fox",
        start_offset=0,
        end_offset=5,  # points to "The q", not "brown fox"
    )
    with pytest.raises(CitationValidationError, match="offset slice"):
        CitationValidator.validate_citation(citation, {"src_1": source})


def test_citation_validator_rejects_out_of_bounds_offsets():
    """Offsets beyond the source text bounds raise CitationValidationError."""
    text = "Short text."
    source = make_source("src_1", text)

    citation = Citation(
        source_id="src_1",
        source_title=source.page_title,
        canonical_url=source.canonical_url,
        domain=source.domain,
        retrieval_date=source.retrieval_timestamp,
        relevant_excerpt="Short text.",
        start_offset=0,
        end_offset=9999,
    )
    with pytest.raises(CitationValidationError, match="out of bounds"):
        CitationValidator.validate_citation(citation, {"src_1": source})


def test_extract_evidence_excerpts_preserves_traceable_offsets_when_truncated():
    """When a paragraph exceeds 1200 chars and is truncated, offsets strictly match the excerpt."""
    long_paragraph = "KeywordMatch: " + ("abcdefghij " * 200)  # > 2000 chars
    source = make_source("src_1", long_paragraph)
    query = ResearchQuery(query="KeywordMatch")

    excerpts = extract_evidence_excerpts(source, query)
    assert len(excerpts) >= 1

    exc = excerpts[0]
    assert len(exc.exact_text) <= 1200
    assert exc.start_offset is not None and exc.end_offset is not None

    # Verify that source[start:end] strictly equals exact_text!
    sliced_from_source = source.extracted_text[exc.start_offset:exc.end_offset]
    assert sliced_from_source == exc.exact_text


def test_duplicate_sources_and_canonical_urls_deduplicated():
    """Sources with identical canonical URLs or identical content hashes are deduplicated."""
    src1 = ResearchSource(
        source_id="src_1",
        canonical_url="https://example.com/docs?utm_source=twitter",
        original_url="https://example.com/docs?utm_source=twitter",
        page_title="Docs 1",
        domain="example.com",
        retrieval_timestamp="2026-01-01T00:00:00Z",
        content_type="text/html",
        extracted_text="Identical content.",
        content_hash="same_hash",
    )
    src2 = ResearchSource(
        source_id="src_2",
        canonical_url="https://example.com/docs?utm_source=email&ref=123",
        original_url="https://example.com/docs?utm_source=email&ref=123",
        page_title="Docs 2",
        domain="example.com",
        retrieval_timestamp="2026-01-01T00:00:00Z",
        content_type="text/html",
        extracted_text="Identical content.",
        content_hash="same_hash",
    )
    query = ResearchQuery(query="content")
    deduped = rank_and_deduplicate_sources([src1, src2], query)
    assert len(deduped) == 1


def test_detects_conflicting_source_content():
    """Detects version discrepancies and deprecation warnings across sources."""
    src_v1 = make_source("src_1", "Package guide for v1.0. Use old_method().")
    src_v2 = make_source("src_2", "Package guide for v2.0. old_method() is deprecated and removed in v2.0.")

    report = ResearchReport(
        query=ResearchQuery(query="method guide"),
        sources=[src_v1, src_v2],
        citations=[
            Citation(
                source_id="src_1",
                source_title="Guide v1",
                canonical_url=src_v1.canonical_url,
                domain=src_v1.domain,
                retrieval_date="2026-01-01T00:00:00Z",
                relevant_excerpt="Package guide for v1.0",
            ),
            Citation(
                source_id="src_2",
                source_title="Guide v2",
                canonical_url=src_v2.canonical_url,
                domain=src_v2.domain,
                retrieval_date="2026-01-01T00:00:00Z",
                relevant_excerpt="Package guide for v2.0",
            ),
        ],
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = ResearchService(Path(tmp_dir))
        service._synthesize_report_findings(report)

        assert len(report.conflicting_sources) >= 1
        # Check that conflicts note versions or deprecation
        conflict_issues = [c.get("issue", "") for c in report.conflicting_sources]
        assert any("version" in issue.lower() or "deprecation" in issue.lower() for issue in conflict_issues)


def test_unsupported_claims_marked_unverified():
    """Keyword matches without formal LLM entailment verification are marked is_verified=False."""
    src = make_source("src_1", "FastAPI supports asynchronous request handling.")
    query = ResearchQuery(query="asynchronous request")
    excerpts = extract_evidence_excerpts(src, query)

    citation = Citation(
        source_id="src_1",
        source_title=src.page_title,
        canonical_url=src.canonical_url,
        domain=src.domain,
        retrieval_date=src.retrieval_timestamp,
        relevant_excerpt=excerpts[0].exact_text,
    )
    # Default is_verified must be False (unverified)
    assert citation.is_verified is False


def test_persisted_and_reloaded_citation_consistency():
    """Ensure citation IDs, source IDs, excerpts, offsets, and verification status persist faithfully."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ResearchStore(Path(tmp_dir))

        src = make_source("src_1", "Deterministic SQLite persistence test string.")
        exc = EvidenceExcerpt(
            source_id="src_1",
            exact_text="SQLite persistence",
            start_offset=14,
            end_offset=32,
            relevance_score=95.0,
        )
        cit = Citation(
            source_id="src_1",
            source_title=src.page_title,
            canonical_url=src.canonical_url,
            domain=src.domain,
            retrieval_date=src.retrieval_timestamp,
            relevant_excerpt="SQLite persistence",
            start_offset=14,
            end_offset=32,
            is_verified=False,
        )
        report = ResearchReport(
            query=ResearchQuery(query="SQLite persistence"),
            sources=[src],
            evidence_excerpts=[exc],
            citations=[cit],
        )

        store.save_report(report)
        reloaded = store.get_report(report.research_id)
        assert reloaded is not None
        assert len(reloaded.citations) == 1

        reloaded_cit = reloaded.citations[0]
        assert reloaded_cit.source_id == "src_1"
        assert reloaded_cit.relevant_excerpt == "SQLite persistence"
        assert reloaded_cit.start_offset == 14
        assert reloaded_cit.end_offset == 32
        assert reloaded_cit.is_verified is False
