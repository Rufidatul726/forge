from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

from forge.config import forge_directory
from forge.research.models import RetrievedDocument

logger = logging.getLogger("forge.research.cache")

DEFAULT_CACHE_TTL_DAYS = 7
DEFAULT_MAX_DOCUMENTS = 500
DEFAULT_MAX_CACHE_BYTES = 50 * 1024 * 1024  # 50 MiB


def normalize_cache_url(url: str) -> str:
    """Normalize URL by stripping fragments and trailing slashes for stable cache keying."""
    parsed = urlsplit(url.strip())
    path = parsed.path.rstrip("/") if parsed.path != "/" else "/"
    normalized = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))
    return normalized


def compute_url_hash(url: str) -> str:
    return hashlib.sha256(normalize_cache_url(url).encode("utf-8")).hexdigest()


class ResearchCache:
    """Persistent SQLite-backed cache for retrieved documentation with TTL and eviction."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        ttl_days: int = DEFAULT_CACHE_TTL_DAYS,
        max_documents: int = DEFAULT_MAX_DOCUMENTS,
        max_cache_bytes: int = DEFAULT_MAX_CACHE_BYTES,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        forge_dir = forge_directory(self.workspace_root)
        forge_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = forge_dir / "research.sqlite3"
        self.ttl_days = max(1, ttl_days)
        self.max_documents = max(1, max_documents)
        self.max_cache_bytes = max(1024, max_cache_bytes)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
        )
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=15000;")
            yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cached_documents (
                        url_hash TEXT PRIMARY KEY,
                        canonical_url TEXT NOT NULL,
                        original_url TEXT NOT NULL,
                        domain TEXT NOT NULL,
                        title TEXT NOT NULL,
                        content_type TEXT NOT NULL,
                        extracted_text TEXT NOT NULL,
                        headings TEXT NOT NULL,
                        code_blocks TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        retrieval_timestamp TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        publication_date TEXT,
                        modification_date TEXT,
                        extraction_warnings TEXT NOT NULL
                    );
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cached_docs_expires ON cached_documents(expires_at);"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cached_docs_timestamp ON cached_documents(retrieval_timestamp);"
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def get(self, url: str) -> RetrievedDocument | None:
        """Retrieve a cached document if it exists and has not expired."""
        url_hash = compute_url_hash(url)
        now_iso = datetime.now(UTC).isoformat()

        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT canonical_url, original_url, domain, title, content_type,
                       extracted_text, headings, code_blocks, content_hash,
                       retrieval_timestamp, expires_at, publication_date,
                       modification_date, extraction_warnings
                FROM cached_documents
                WHERE url_hash = ?;
                """,
                (url_hash,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            expires_at = row[10]
            if expires_at < now_iso:
                # Expired - remove from cache
                conn.execute("DELETE FROM cached_documents WHERE url_hash = ?;", (url_hash,))
                return None

            return RetrievedDocument(
                canonical_url=row[0],
                original_url=row[1],
                domain=row[2],
                title=row[3],
                content_type=row[4],
                extracted_text=row[5],
                headings=json.loads(row[6]),
                code_blocks=json.loads(row[7]),
                content_hash=row[8],
                retrieval_timestamp=row[9],
                status="cached",
                publication_date=row[11],
                modification_date=row[12],
                extraction_warnings=json.loads(row[13]),
            )

    def put(self, doc: RetrievedDocument, ttl_days: int | None = None) -> None:
        """Store or update a retrieved document in the cache with expiration and eviction."""
        effective_ttl = ttl_days or self.ttl_days
        now = datetime.now(UTC)
        expires_at = (now + timedelta(days=effective_ttl)).isoformat()
        url_hash = compute_url_hash(doc.original_url)

        doc_json = doc.extracted_text.encode("utf-8")
        size_bytes = len(doc_json)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO cached_documents
                    (url_hash, canonical_url, original_url, domain, title, content_type,
                     extracted_text, headings, code_blocks, content_hash, retrieval_timestamp,
                     expires_at, size_bytes, publication_date, modification_date, extraction_warnings)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        url_hash,
                        doc.canonical_url,
                        doc.original_url,
                        doc.domain,
                        doc.title,
                        doc.content_type,
                        doc.extracted_text,
                        json.dumps(doc.headings),
                        json.dumps(doc.code_blocks),
                        doc.content_hash,
                        doc.retrieval_timestamp,
                        expires_at,
                        size_bytes,
                        doc.publication_date,
                        doc.modification_date,
                        json.dumps(doc.extraction_warnings),
                    ),
                )

                # Check and enforce cache bounds (max documents and max bytes)
                self._enforce_bounds(conn)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def _enforce_bounds(self, conn: sqlite3.Connection) -> None:
        """Evict oldest entries when document count or total bytes exceed limits."""
        cursor = conn.execute("SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM cached_documents;")
        count, total_bytes = cursor.fetchone()

        while count > self.max_documents or total_bytes > self.max_cache_bytes:
            # Delete oldest document
            oldest_cur = conn.execute(
                "SELECT url_hash, size_bytes FROM cached_documents ORDER BY retrieval_timestamp ASC, rowid ASC LIMIT 1;"
            )
            oldest = oldest_cur.fetchone()
            if not oldest:
                break
            old_hash, old_size = oldest
            conn.execute("DELETE FROM cached_documents WHERE url_hash = ?;", (old_hash,))
            count -= 1
            total_bytes -= old_size

    def prune_expired(self) -> int:
        """Delete all expired documents from cache. Return count deleted."""
        now_iso = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cursor = conn.execute("DELETE FROM cached_documents WHERE expires_at < ?;", (now_iso,))
                deleted = cursor.rowcount
                conn.execute("COMMIT;")
                return deleted
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def clear(self) -> int:
        """Clear all cached documents completely. Return count deleted."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cursor = conn.execute("DELETE FROM cached_documents;")
                deleted = cursor.rowcount
                conn.execute("COMMIT;")
                return deleted
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def status(self) -> dict[str, Any]:
        """Return cache health, statistics, document count, and byte usage."""
        now_iso = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(size_bytes), 0),
                       MIN(retrieval_timestamp), MAX(retrieval_timestamp)
                FROM cached_documents;
                """
            )
            total_docs, total_bytes, oldest, newest = cursor.fetchone()

            exp_cur = conn.execute(
                "SELECT COUNT(*) FROM cached_documents WHERE expires_at < ?;",
                (now_iso,),
            )
            expired_docs = exp_cur.fetchone()[0]

            return {
                "total_documents": total_docs,
                "total_bytes": total_bytes,
                "total_size_mb": round(total_bytes / (1024 * 1024), 2),
                "expired_documents": expired_docs,
                "max_documents_limit": self.max_documents,
                "max_bytes_limit_mb": round(self.max_cache_bytes / (1024 * 1024), 2),
                "ttl_days": self.ttl_days,
                "oldest_entry": oldest,
                "newest_entry": newest,
            }
