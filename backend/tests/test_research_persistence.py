from datetime import UTC, datetime
from pathlib import Path

from forge.research.cache import ResearchCache
from forge.research.models import (
    Citation,
    EvidenceExcerpt,
    ResearchQuery,
    ResearchReport,
    ResearchSource,
    ResearchStatus,
    RetrievedDocument,
)
from forge.research.storage import ResearchStore


def test_research_store_save_and_get_report(tmp_path: Path):
    store = ResearchStore(tmp_path)

    query = ResearchQuery(query="FastAPI security", limit=3, domain_restrictions=["fastapi.tiangolo.com"])
    source = ResearchSource(
        source_id="src_1",
        canonical_url="https://fastapi.tiangolo.com/tutorial/security/",
        original_url="https://fastapi.tiangolo.com/tutorial/security/",
        page_title="FastAPI Security Tutorial",
        domain="fastapi.tiangolo.com",
        retrieval_timestamp=datetime.now(UTC).isoformat(),
        content_type="text/html",
        extracted_text="FastAPI provides OAuth2 with password bearer flow.",
        content_hash="abc123hash",
        retrieval_status="success",
        search_rank=1,
        relevance_score=85.5,
        source_type="official_doc",
        ranking_signals={"title_relevance": 30.0, "official_doc_preference": 15.0},
    )
    excerpt = EvidenceExcerpt(
        source_id="src_1",
        exact_text="OAuth2 with password bearer flow",
        section_heading="OAuth2",
        start_offset=10,
        end_offset=42,
        relevance_score=90.0,
        relevance_reasons=["Direct keyword match"],
    )
    citation = Citation(
        source_id="src_1",
        source_title="FastAPI Security Tutorial",
        canonical_url="https://fastapi.tiangolo.com/tutorial/security/",
        domain="fastapi.tiangolo.com",
        retrieval_date=source.retrieval_timestamp,
        relevant_excerpt="OAuth2 with password bearer flow",
        section_heading="OAuth2",
        is_verified=True,
    )

    report = ResearchReport(
        research_id="rep_test_01",
        query=query,
        sources=[source],
        evidence_excerpts=[excerpt],
        citations=[citation],
        verified_facts=["FastAPI supports OAuth2 natively."],
        inferences=["Bearer tokens can be validated securely."],
        recommendations=["Use Depends(OAuth2PasswordBearer)."],
        summary="Retrieved 1 source.",
        status=ResearchStatus.COMPLETED,
    )

    store.save_report(report)

    loaded = store.get_report("rep_test_01")
    assert loaded is not None
    assert loaded.research_id == "rep_test_01"
    assert loaded.query.query == "FastAPI security"
    assert len(loaded.sources) == 1
    assert loaded.sources[0].source_id == "src_1"
    assert loaded.sources[0].ranking_signals["title_relevance"] == 30.0
    assert len(loaded.evidence_excerpts) == 1
    assert loaded.evidence_excerpts[0].exact_text == "OAuth2 with password bearer flow"
    assert len(loaded.citations) == 1
    assert loaded.citations[0].citation_id == citation.citation_id

    reports_list = store.list_reports()
    assert len(reports_list) == 1
    assert reports_list[0]["research_id"] == "rep_test_01"


def test_research_cache_ttl_and_eviction(tmp_path: Path):
    cache = ResearchCache(tmp_path, ttl_days=1, max_documents=2, max_cache_bytes=10_000)

    doc1 = RetrievedDocument(
        canonical_url="https://example.com/doc1",
        original_url="https://example.com/doc1",
        title="Doc 1",
        domain="example.com",
        content_type="text/html",
        extracted_text="Content for document 1",
        content_hash="h1",
        retrieval_timestamp="2026-01-01T00:00:00Z",
    )
    doc2 = RetrievedDocument(
        canonical_url="https://example.com/doc2",
        original_url="https://example.com/doc2",
        title="Doc 2",
        domain="example.com",
        content_type="text/html",
        extracted_text="Content for document 2",
        content_hash="h2",
        retrieval_timestamp="2026-01-01T00:00:01Z",
    )
    doc3 = RetrievedDocument(
        canonical_url="https://example.com/doc3",
        original_url="https://example.com/doc3",
        title="Doc 3",
        domain="example.com",
        content_type="text/html",
        extracted_text="Content for document 3",
        content_hash="h3",
        retrieval_timestamp="2026-01-01T00:00:02Z",
    )

    cache.put(doc1)
    cache.put(doc2)

    assert cache.get("https://example.com/doc1") is not None
    assert cache.get("https://example.com/doc2") is not None

    # Adding doc3 should evict the oldest document (doc1) because max_documents=2
    cache.put(doc3)
    assert cache.get("https://example.com/doc1") is None
    assert cache.get("https://example.com/doc3") is not None

    # Check status
    st = cache.status()
    assert st["total_documents"] == 2

    # Clear cache
    deleted = cache.clear()
    assert deleted == 2
    assert cache.status()["total_documents"] == 0
