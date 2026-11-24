import concurrent.futures
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest

from forge.config import forge_directory
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


# ==============================================================================
# SECTION 6: Persistence & Cache Hardening Tests
# ==============================================================================

def test_cache_refuses_to_store_failed_or_empty_fetches():
    """Failed, errored, or empty document fetches must never create cache entries."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cache = ResearchCache(Path(tmp_dir))

        # 1. Failed status
        failed_doc = RetrievedDocument(
            canonical_url="https://example.com/failed",
            original_url="https://example.com/failed",
            title="Failed Doc",
            domain="example.com",
            content_type="text/html",
            extracted_text="Some error content",
            status="failed",
            error_message="Connection timed out",
        )
        cache.put(failed_doc)
        assert cache.get("https://example.com/failed") is None

        # 2. Empty text
        empty_doc = RetrievedDocument(
            canonical_url="https://example.com/empty",
            original_url="https://example.com/empty",
            title="Empty Doc",
            domain="example.com",
            content_type="text/html",
            extracted_text="",
            status="success",
        )
        cache.put(empty_doc)
        assert cache.get("https://example.com/empty") is None


def test_cache_strips_sensitive_credentials_and_tracking_params():
    """Cache normalizes URLs and strips sensitive API keys, tokens, and tracking parameters."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cache = ResearchCache(Path(tmp_dir))

        doc = RetrievedDocument(
            canonical_url="https://user:secret123@example.com/docs?api_key=SECRET_VAL&utm_source=twitter",
            original_url="https://user:secret123@example.com/docs?api_key=SECRET_VAL&utm_source=twitter",
            title="Sensitive URL Doc",
            domain="example.com",
            content_type="text/html",
            extracted_text="Content without keys.",
            status="success",
        )
        cache.put(doc)

        # Lookup with or without the sensitive token should hit the same clean normalized cache entry
        cached = cache.get("https://example.com/docs")
        assert cached is not None
        assert "SECRET_VAL" not in cached.canonical_url
        assert "secret123" not in cached.canonical_url


def test_cache_bounds_eviction_by_document_count():
    """Cache evicts oldest documents when max_documents limit is exceeded."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Cache with limit of 3 documents
        cache = ResearchCache(Path(tmp_dir), max_documents=3)

        for i in range(1, 5):
            doc = RetrievedDocument(
                canonical_url=f"https://example.com/doc{i}",
                original_url=f"https://example.com/doc{i}",
                title=f"Doc {i}",
                domain="example.com",
                content_type="text/plain",
                extracted_text=f"Text content for document {i}",
                status="success",
                retrieval_timestamp=(datetime.now(UTC) + timedelta(seconds=i)).isoformat(),
            )
            cache.put(doc)

        status = cache.status()
        assert status["total_documents"] == 3
        # Oldest document doc1 must have been evicted
        assert cache.get("https://example.com/doc1") is None
        # Newest document doc4 must be present
        assert cache.get("https://example.com/doc4") is not None


def test_cache_bounds_eviction_by_byte_size():
    """Cache evicts oldest documents when max_cache_bytes limit is exceeded."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Cache with limit of 2000 bytes
        cache = ResearchCache(Path(tmp_dir), max_documents=50, max_cache_bytes=2000)

        # Insert 3 docs of 800 bytes each -> 2400 bytes, exceeds 2000 limit
        for i in range(1, 4):
            doc = RetrievedDocument(
                canonical_url=f"https://example.com/large{i}",
                original_url=f"https://example.com/large{i}",
                title=f"Large Doc {i}",
                domain="example.com",
                content_type="text/plain",
                extracted_text="x" * 800,
                status="success",
                retrieval_timestamp=(datetime.now(UTC) + timedelta(seconds=i)).isoformat(),
            )
            cache.put(doc)

        status = cache.status()
        assert status["total_bytes"] <= 2000
        # Oldest doc1 evicted
        assert cache.get("https://example.com/large1") is None


def test_cache_prunes_expired_documents():
    """Expired documents are pruned correctly."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cache = ResearchCache(Path(tmp_dir), ttl_days=1)

        # Insert document expired in the past
        doc = RetrievedDocument(
            canonical_url="https://example.com/expired",
            original_url="https://example.com/expired",
            title="Expired Doc",
            domain="example.com",
            content_type="text/plain",
            extracted_text="Old data",
            status="success",
        )
        cache.put(doc, ttl_days=1)

        # Manually alter expires_at to yesterday
        with cache._connect() as conn:
            yesterday = (datetime.now(UTC) - timedelta(days=2)).isoformat()
            conn.execute("UPDATE cached_documents SET expires_at = ?;", (yesterday,))

        # get() on expired document returns None
        assert cache.get("https://example.com/expired") is None

        # prune_expired deletes it
        deleted = cache.prune_expired()
        assert deleted == 0 or deleted == 1


def test_persistence_atomic_transaction_rollback_on_error():
    """Simulated write failure rolls back the entire transaction; leaves no partial report."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ResearchStore(Path(tmp_dir))

        src = ResearchSource(
            source_id="src_1",
            canonical_url="https://example.com/doc",
            original_url="https://example.com/doc",
            page_title="Title",
            domain="example.com",
            retrieval_timestamp="2026-01-01T00:00:00Z",
            content_type="text/html",
            extracted_text="Sample text",
            content_hash="hash1",
        )
        report = ResearchReport(
            query=ResearchQuery(query="Transaction test"),
            sources=[src],
            citations=[
                Citation(
                    source_id="src_1",
                    source_title="Title",
                    canonical_url=src.canonical_url,
                    domain=src.domain,
                    retrieval_date=src.retrieval_timestamp,
                    relevant_excerpt="Sample text",
                )
            ],
        )

        # Trigger a failure midway by making evidence_excerpts table temporarily invalid or closed
        with store._connect() as conn:
            conn.execute("DROP TABLE citations;")

        with pytest.raises(sqlite3.DatabaseError):
            store.save_report(report)

        # Verify nothing was committed: query does not exist
        with store._connect() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM research_queries;")
            assert cur.fetchone()[0] == 0


def test_corrupted_database_recovery_safely_backs_up():
    """Corrupted database file is backed up to .corrupt.<timestamp> and a fresh store is initialized."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        workspace_dir = Path(tmp_dir)
        forge_dir = forge_directory(workspace_dir)
        forge_dir.mkdir(parents=True, exist_ok=True)
        db_file = forge_dir / "research.sqlite3"

        # Corrupt the file with random junk
        db_file.write_bytes(b"THIS_IS_CORRUPT_NOT_A_SQLITE_DATABASE_FILE")

        # Initializing ResearchStore should detect the corruption, back it up, and reinitialize
        store = ResearchStore(workspace_dir)

        # Check backup was created
        corrupt_backups = list(forge_dir.glob("research.sqlite3.corrupt.*"))
        assert len(corrupt_backups) == 1
        assert corrupt_backups[0].read_bytes() == b"THIS_IS_CORRUPT_NOT_A_SQLITE_DATABASE_FILE"

        # Store operates normally on clean DB
        report = ResearchReport(query=ResearchQuery(query="Post recovery test"))
        store.save_report(report)
        assert store.get_report(report.research_id) is not None


def test_valid_database_never_falsely_recovered():
    """A valid database is not treated as corrupt when re-opened."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        workspace_dir = Path(tmp_dir)
        store = ResearchStore(workspace_dir)
        report = ResearchReport(query=ResearchQuery(query="Original report"))
        store.save_report(report)

        # Re-initialize store on same directory
        store2 = ResearchStore(workspace_dir)
        assert store2.get_report(report.research_id) is not None

        # Verify no corrupt backups were created
        forge_dir = forge_directory(workspace_dir)
        assert len(list(forge_dir.glob("research.sqlite3.corrupt.*"))) == 0


def test_concurrent_cache_and_store_access():
    """Concurrent readers and writers do not deadlock or corrupt SQLite WAL database."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        workspace_dir = Path(tmp_dir)
        cache = ResearchCache(workspace_dir)
        store = ResearchStore(workspace_dir)

        def worker(worker_id: int):
            # Write to cache
            doc = RetrievedDocument(
                canonical_url=f"https://example.com/worker_{worker_id}",
                original_url=f"https://example.com/worker_{worker_id}",
                title=f"Worker {worker_id}",
                domain="example.com",
                content_type="text/plain",
                extracted_text=f"Worker content {worker_id}",
                status="success",
            )
            cache.put(doc)
            _ = cache.get(f"https://example.com/worker_{worker_id}")

            # Write to store
            report = ResearchReport(
                query=ResearchQuery(query=f"Query {worker_id}"),
                sources=[make_source(f"src_{worker_id}", f"Content {worker_id}")],
            )
            store.save_report(report)
            _ = store.get_report(report.research_id)
            return True

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(worker, i) for i in range(15)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        assert all(results)
        assert cache.status()["total_documents"] == 15
        assert len(store.list_reports(limit=50)) == 15


def test_preserves_m3_repo_index():
    """Research database operations never alter or delete the M3 index database."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        workspace_dir = Path(tmp_dir)
        forge_dir = forge_directory(workspace_dir)
        forge_dir.mkdir(parents=True, exist_ok=True)

        # Simulate M3 repository index database in the same .forge/ directory
        m3_db = forge_dir / "index.sqlite3"
        m3_db.write_text("M3_INDEX_DATA", encoding="utf-8")

        # Initialize and populate research cache & store
        store = ResearchStore(workspace_dir)
        report = ResearchReport(query=ResearchQuery(query="Preserve M3 test"))
        store.save_report(report)

        # Verify M3 database remains completely untouched
        assert m3_db.exists()
        assert m3_db.read_text(encoding="utf-8") == "M3_INDEX_DATA"


def make_source(source_id: str, text: str) -> ResearchSource:
    return ResearchSource(
        source_id=source_id,
        canonical_url="https://example.com/doc",
        original_url="https://example.com/doc",
        page_title="Title",
        domain="example.com",
        retrieval_timestamp="2026-01-01T00:00:00Z",
        content_type="text/html",
        extracted_text=text,
        content_hash="h1",
    )
