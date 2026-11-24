from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from forge.config import forge_directory
from forge.research.models import (
    Citation,
    EvidenceExcerpt,
    FindingKind,
    ResearchFinding,
    ResearchQuery,
    ResearchReport,
    ResearchSource,
    ResearchStatus,
)

logger = logging.getLogger("forge.research.storage")

CURRENT_SCHEMA_VERSION = 1

SENSITIVE_URL_PARAMS = {
    "api_key",
    "apikey",
    "access_token",
    "token",
    "secret",
    "auth",
    "password",
    "key",
}


def sanitize_url_credentials(url: str) -> str:
    """Strip embedded credentials and sensitive auth query params from stored URLs."""
    if not url:
        return ""
    parsed = urlsplit(url.strip())
    netloc = parsed.netloc.split("@")[-1]
    filtered_params = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in SENSITIVE_URL_PARAMS
    ]
    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(filtered_params), parsed.fragment))


class ResearchStorageError(Exception):
    """Base error for research storage operations."""


class ResearchStore:
    """SQLite-backed transactional persistence for research queries, sources, and reports."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        forge_dir = forge_directory(self.workspace_root)
        forge_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = forge_dir / "research.sqlite3"
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,  # Autocommit mode, we manage transactions explicitly
        )
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=15000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            yield conn
        finally:
            conn.close()

    def _is_database_corrupted(self) -> bool:
        if not self.db_path.exists():
            return False
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=5.0)
            try:
                cur = conn.execute("PRAGMA integrity_check;")
                row = cur.fetchone()
                return not (row and row[0] == "ok")
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return True
        except Exception:
            return False

    def _initialize(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS schema_migrations (
                            version INTEGER PRIMARY KEY,
                            applied_at TEXT NOT NULL
                        );
                        """
                    )
                    cursor = conn.execute("SELECT MAX(version) FROM schema_migrations;")
                    row = cursor.fetchone()
                    current_version = row[0] if (row and row[0] is not None) else 0

                    if current_version < 1:
                        self._apply_migration_v1(conn)

                    # Ensure start_offset and end_offset exist in citations table
                    try:
                        conn.execute("ALTER TABLE citations ADD COLUMN start_offset INTEGER;")
                    except sqlite3.OperationalError:
                        pass
                    try:
                        conn.execute("ALTER TABLE citations ADD COLUMN end_offset INTEGER;")
                    except sqlite3.OperationalError:
                        pass

                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise
        except sqlite3.DatabaseError as exc:
            if self._is_database_corrupted():
                logger.error("Corrupted research database detected by integrity check, attempting recovery: %s", exc)
                self._recover_corrupted_database()
            else:
                logger.warning("Database error during init (not corrupt): %s", exc)
                raise

    def _recover_corrupted_database(self) -> None:
        if self.db_path.exists():
            backup_path = self.db_path.with_name(
                f"{self.db_path.name}.corrupt.{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
            )
            try:
                self.db_path.rename(backup_path)
            except OSError as exc:
                logger.warning("Could not rename corrupted database: %s", exc)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    );
                    """
                )
                self._apply_migration_v1(conn)
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def _apply_migration_v1(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS research_queries (
                id TEXT PRIMARY KEY,
                query_text TEXT NOT NULL,
                limit_val INTEGER NOT NULL,
                domain_restrictions TEXT NOT NULL,
                recency TEXT,
                package TEXT,
                version TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS research_sources (
                id TEXT PRIMARY KEY,
                canonical_url TEXT NOT NULL,
                original_url TEXT NOT NULL,
                page_title TEXT NOT NULL,
                domain TEXT NOT NULL,
                publication_date TEXT,
                retrieval_timestamp TEXT NOT NULL,
                content_type TEXT NOT NULL,
                extracted_text TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                retrieval_status TEXT NOT NULL,
                search_rank INTEGER,
                relevance_score REAL NOT NULL,
                source_type TEXT NOT NULL,
                extraction_warnings TEXT NOT NULL,
                ranking_signals TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS research_query_sources (
                query_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                rank_order INTEGER NOT NULL,
                PRIMARY KEY (query_id, source_id),
                FOREIGN KEY (query_id) REFERENCES research_queries(id) ON DELETE CASCADE,
                FOREIGN KEY (source_id) REFERENCES research_sources(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_excerpts (
                id TEXT PRIMARY KEY,
                report_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                exact_text TEXT NOT NULL,
                section_heading TEXT,
                start_offset INTEGER,
                end_offset INTEGER,
                relevance_score REAL NOT NULL,
                relevance_reasons TEXT NOT NULL,
                supporting_claim TEXT
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS research_reports (
                id TEXT PRIMARY KEY,
                query_id TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                verified_facts TEXT NOT NULL,
                inferences TEXT NOT NULL,
                recommendations TEXT NOT NULL,
                unverified_assumptions TEXT NOT NULL,
                unresolved_questions TEXT NOT NULL,
                conflicting_sources TEXT NOT NULL,
                warnings TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (query_id) REFERENCES research_queries(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS citations (
                id TEXT PRIMARY KEY,
                report_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_title TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                domain TEXT NOT NULL,
                retrieval_date TEXT NOT NULL,
                relevant_excerpt TEXT NOT NULL,
                section_heading TEXT,
                is_verified INTEGER NOT NULL,
                start_offset INTEGER,
                end_offset INTEGER,
                FOREIGN KEY (report_id) REFERENCES research_reports(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, applied_at) VALUES (1, ?);",
            (datetime.now(UTC).isoformat(),),
        )

    def save_report(self, report: ResearchReport) -> None:
        """Persist a complete research report, query, sources, excerpts, and citations atomically."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                # 1. Save Query
                q = report.query
                query_id = f"qry_{report.research_id[:12]}"
                conn.execute(
                    """
                    INSERT OR REPLACE INTO research_queries
                    (id, query_text, limit_val, domain_restrictions, recency, package, version, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        query_id,
                        q.query,
                        q.limit,
                        json.dumps(q.domain_restrictions),
                        q.recency,
                        q.package,
                        q.version,
                        q.created_at,
                    ),
                )

                # 2. Save Sources and query_sources links
                for rank_idx, s in enumerate(report.sources, start=1):
                    clean_s_canonical = sanitize_url_credentials(s.canonical_url)
                    clean_s_original = sanitize_url_credentials(s.original_url)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO research_sources
                        (id, canonical_url, original_url, page_title, domain, publication_date,
                         retrieval_timestamp, content_type, extracted_text, content_hash,
                         retrieval_status, search_rank, relevance_score, source_type,
                         extraction_warnings, ranking_signals)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            s.source_id,
                            clean_s_canonical,
                            clean_s_original,
                            s.page_title,
                            s.domain,
                            s.publication_or_modification_date,
                            s.retrieval_timestamp,
                            s.content_type,
                            s.extracted_text,
                            s.content_hash,
                            s.retrieval_status,
                            s.search_rank,
                            s.relevance_score,
                            s.source_type,
                            json.dumps(s.extraction_warnings),
                            json.dumps(s.ranking_signals),
                        ),
                    )
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO research_query_sources
                        (query_id, source_id, rank_order)
                        VALUES (?, ?, ?);
                        """,
                        (query_id, s.source_id, rank_idx),
                    )

                # 3. Save Report
                conn.execute(
                    """
                    INSERT OR REPLACE INTO research_reports
                    (id, query_id, status, summary, verified_facts, inferences, recommendations,
                     unverified_assumptions, unresolved_questions, conflicting_sources, warnings, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        report.research_id,
                        query_id,
                        report.status.value if isinstance(report.status, ResearchStatus) else str(report.status),
                        report.summary,
                        json.dumps(report.verified_facts),
                        json.dumps(report.inferences),
                        json.dumps(report.recommendations),
                        json.dumps(report.unverified_assumptions),
                        json.dumps(report.unresolved_questions),
                        json.dumps(report.conflicting_sources),
                        json.dumps(report.warnings),
                        report.created_at,
                    ),
                )

                # 4. Save Evidence Excerpts
                for exc in report.evidence_excerpts:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO evidence_excerpts
                        (id, report_id, source_id, exact_text, section_heading, start_offset,
                         end_offset, relevance_score, relevance_reasons, supporting_claim)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            exc.excerpt_id,
                            report.research_id,
                            exc.source_id,
                            exc.exact_text,
                            exc.section_heading,
                            exc.start_offset,
                            exc.end_offset,
                            exc.relevance_score,
                            json.dumps(exc.relevance_reasons),
                            exc.supporting_claim,
                        ),
                    )

                # 5. Save Citations
                for cit in report.citations:
                    clean_cit_url = sanitize_url_credentials(cit.canonical_url)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO citations
                        (id, report_id, source_id, source_title, canonical_url, domain,
                         retrieval_date, relevant_excerpt, section_heading, is_verified,
                         start_offset, end_offset)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            cit.citation_id,
                            report.research_id,
                            cit.source_id,
                            cit.source_title,
                            clean_cit_url,
                            cit.domain,
                            cit.retrieval_date,
                            cit.relevant_excerpt,
                            cit.section_heading,
                            1 if cit.is_verified else 0,
                            cit.start_offset,
                            cit.end_offset,
                        ),
                    )

                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def get_report(self, report_id: str) -> ResearchReport | None:
        """Load a persisted research report along with its query, sources, excerpts, and citations."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT id, query_id, status, summary, verified_facts, inferences, recommendations,
                       unverified_assumptions, unresolved_questions, conflicting_sources, warnings, created_at
                FROM research_reports
                WHERE id = ?;
                """,
                (report_id,),
            )
            report_row = cursor.fetchone()
            if not report_row:
                return None

            (
                r_id,
                q_id,
                status_str,
                summary,
                v_facts,
                infs,
                recs,
                unver_assumps,
                unres_qs,
                conflicts,
                warns,
                created_at,
            ) = report_row

            # Fetch Query
            q_cursor = conn.execute(
                """
                SELECT query_text, limit_val, domain_restrictions, recency, package, version, created_at
                FROM research_queries
                WHERE id = ?;
                """,
                (q_id,),
            )
            q_row = q_cursor.fetchone()
            if q_row:
                query_obj = ResearchQuery(
                    query=q_row[0],
                    limit=q_row[1],
                    domain_restrictions=json.loads(q_row[2]),
                    recency=q_row[3],
                    package=q_row[4],
                    version=q_row[5],
                    created_at=q_row[6],
                )
            else:
                query_obj = ResearchQuery(query="Unknown")

            # Fetch Sources
            s_cursor = conn.execute(
                """
                SELECT s.id, s.canonical_url, s.original_url, s.page_title, s.domain,
                       s.publication_date, s.retrieval_timestamp, s.content_type, s.extracted_text,
                       s.content_hash, s.retrieval_status, s.search_rank, s.relevance_score,
                       s.source_type, s.extraction_warnings, s.ranking_signals
                FROM research_sources s
                JOIN research_query_sources qs ON s.id = qs.source_id
                WHERE qs.query_id = ?
                ORDER BY qs.rank_order ASC;
                """,
                (q_id,),
            )
            sources: list[ResearchSource] = []
            for s_row in s_cursor.fetchall():
                sources.append(
                    ResearchSource(
                        source_id=s_row[0],
                        canonical_url=s_row[1],
                        original_url=s_row[2],
                        page_title=s_row[3],
                        domain=s_row[4],
                        publication_or_modification_date=s_row[5],
                        retrieval_timestamp=s_row[6],
                        content_type=s_row[7],
                        extracted_text=s_row[8],
                        content_hash=s_row[9],
                        retrieval_status=s_row[10],
                        search_rank=s_row[11],
                        relevance_score=s_row[12],
                        source_type=s_row[13],
                        extraction_warnings=json.loads(s_row[14]),
                        ranking_signals=json.loads(s_row[15]),
                    )
                )

            # Fetch Evidence Excerpts
            exc_cursor = conn.execute(
                """
                SELECT id, source_id, exact_text, section_heading, start_offset, end_offset,
                       relevance_score, relevance_reasons, supporting_claim
                FROM evidence_excerpts
                WHERE report_id = ?;
                """,
                (report_id,),
            )
            excerpts: list[EvidenceExcerpt] = []
            for exc_row in exc_cursor.fetchall():
                excerpts.append(
                    EvidenceExcerpt(
                        excerpt_id=exc_row[0],
                        source_id=exc_row[1],
                        exact_text=exc_row[2],
                        section_heading=exc_row[3],
                        start_offset=exc_row[4],
                        end_offset=exc_row[5],
                        relevance_score=exc_row[6],
                        relevance_reasons=json.loads(exc_row[7]),
                        supporting_claim=exc_row[8],
                    )
                )

            # Fetch Citations
            cit_cursor = conn.execute(
                """
                SELECT id, source_id, source_title, canonical_url, domain, retrieval_date,
                       relevant_excerpt, section_heading, is_verified, start_offset, end_offset
                FROM citations
                WHERE report_id = ?;
                """,
                (report_id,),
            )
            citations: list[Citation] = []
            for cit_row in cit_cursor.fetchall():
                citations.append(
                    Citation(
                        citation_id=cit_row[0],
                        source_id=cit_row[1],
                        source_title=cit_row[2],
                        canonical_url=cit_row[3],
                        domain=cit_row[4],
                        retrieval_date=cit_row[5],
                        relevant_excerpt=cit_row[6],
                        section_heading=cit_row[7],
                        is_verified=bool(cit_row[8]),
                        start_offset=cit_row[9] if len(cit_row) > 9 else None,
                        end_offset=cit_row[10] if len(cit_row) > 10 else None,
                    )
                )

            return ResearchReport(
                research_id=r_id,
                query=query_obj,
                sources=sources,
                evidence_excerpts=excerpts,
                citations=citations,
                verified_facts=json.loads(v_facts),
                inferences=json.loads(infs),
                recommendations=json.loads(recs),
                unverified_assumptions=json.loads(unver_assumps),
                unresolved_questions=json.loads(unres_qs),
                conflicting_sources=json.loads(conflicts),
                warnings=json.loads(warns),
                summary=summary,
                created_at=created_at,
                status=ResearchStatus(status_str) if status_str in ResearchStatus._value2member_map_ else ResearchStatus.COMPLETED,
            )

    def list_reports(self, limit: int = 50) -> list[dict[str, Any]]:
        """List summary of recorded reports."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT r.id, q.query_text, r.status, r.created_at, COUNT(c.id)
                FROM research_reports r
                JOIN research_queries q ON r.query_id = q.id
                LEFT JOIN citations c ON r.id = c.report_id
                GROUP BY r.id
                ORDER BY r.created_at DESC
                LIMIT ?;
                """,
                (limit,),
            )
            return [
                {
                    "research_id": row[0],
                    "query": row[1],
                    "status": row[2],
                    "created_at": row[3],
                    "citations_count": row[4],
                }
                for row in cursor.fetchall()
            ]
