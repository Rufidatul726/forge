from forge.research.models import ResearchQuery, ResearchSource
from forge.research.ranking import (
    classify_source_type,
    compute_ranking_signals,
    extract_keywords,
    normalize_url,
    rank_and_deduplicate_sources,
)


def test_normalize_url():
    url1 = "https://example.com/docs/?utm_source=twitter&utm_medium=cpc&ref=123#overview"
    assert normalize_url(url1) == "https://example.com/docs"

    url2 = "http://EXAMPLE.COM/PATH/"
    assert normalize_url(url2) == "http://example.com/PATH"


def test_classify_source_type():
    assert classify_source_type("docs.pydantic.dev", "https://docs.pydantic.dev") == "official_doc"
    assert classify_source_type("github.com", "https://github.com/owner/repo") == "repository"
    assert classify_source_type("stackoverflow.com", "https://stackoverflow.com/questions/123") == "community"
    assert classify_source_type("myblog.com", "https://myblog.com/guide/intro") == "documentation"
    assert classify_source_type("random.com", "https://random.com/page") == "web"


def test_rank_and_deduplicate_sources():
    query = ResearchQuery(query="Pydantic v2 migration", version="v2")

    # Source 1: Highly relevant official doc
    s1 = ResearchSource(
        source_id="src_1",
        canonical_url="https://docs.pydantic.dev/2.0/migration/",
        original_url="https://docs.pydantic.dev/2.0/migration/?utm_source=test",
        page_title="Pydantic V2 Migration Guide",
        domain="docs.pydantic.dev",
        retrieval_timestamp="2026-01-01T00:00:00Z",
        content_type="text/html",
        extracted_text="# Migration to Pydantic v2\nDetails on migrating models.",
        content_hash="hash_pydantic_v2",
        search_rank=1,
    )

    # Source 2: Duplicate of s1 by canonical URL
    s2 = ResearchSource(
        source_id="src_2",
        canonical_url="https://docs.pydantic.dev/2.0/migration/",
        original_url="https://docs.pydantic.dev/2.0/migration/",
        page_title="Pydantic V2 Migration",
        domain="docs.pydantic.dev",
        retrieval_timestamp="2026-01-01T00:00:00Z",
        content_type="text/html",
        extracted_text="# Migration to Pydantic v2\nDetails on migrating models.",
        content_hash="hash_pydantic_v2",
        search_rank=2,
    )

    # Source 3: Generic web source with older version mismatch
    s3 = ResearchSource(
        source_id="src_3",
        canonical_url="https://blog.example.com/pydantic-v1-notes",
        original_url="https://blog.example.com/pydantic-v1-notes",
        page_title="Pydantic v1.10 overview",
        domain="blog.example.com",
        retrieval_timestamp="2026-01-01T00:00:00Z",
        content_type="text/html",
        extracted_text="Older notes on Pydantic v1 models.",
        content_hash="hash_blog_v1",
        search_rank=3,
    )

    ranked = rank_and_deduplicate_sources([s1, s2, s3], query)

    # Deduped: s2 dropped because same canonical URL and hash as s1
    assert len(ranked) == 2
    assert ranked[0].canonical_url == "https://docs.pydantic.dev/2.0/migration/"
    # s1 score higher than s3
    assert ranked[0].relevance_score > ranked[1].relevance_score
    assert "title_relevance" in ranked[0].ranking_signals
    assert "version_match" in ranked[0].ranking_signals
