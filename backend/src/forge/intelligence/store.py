from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from forge.config import forge_directory
from forge.intelligence.models import (
    DependencyRecord,
    ExtractedFileSymbols,
    GitMetadata,
    ImportRecord,
    ReferenceKind,
    ReferenceRecord,
    ScannedFile,
    SymbolKind,
    SymbolRecord,
)

logger = logging.getLogger("forge.intelligence.store")

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class IndexStats:
    total_files: int
    total_symbols: int
    symbols_by_kind: dict[str, int]
    languages: dict[str, int]
    last_run_at: str | None
    last_run_status: str | None
    last_run_duration_ms: float = 0.0


@dataclass(frozen=True)
class SearchSymbolResult:
    name: str
    kind: str
    path: str
    line_number: int
    end_line_number: int
    parent_name: str | None
    signature: str | None
    docstring: str | None


class IndexStore:
    """Versioned SQLite store for persistent repository intelligence."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.db_path = forge_directory(self.workspace_root) / "index.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA journal_mode = WAL;")
            return conn
        except Exception:
            conn.close()
            raise

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._session() as conn:
                check = conn.execute("PRAGMA integrity_check;").fetchone()
                if not check or check[0] != "ok":
                    raise sqlite3.DatabaseError(f"Integrity check failed: {check}")
                self._apply_migrations(conn)
                self._recover_interrupted_runs(conn)
        except sqlite3.DatabaseError as exc:
            logger.warning("Corrupted index database detected (%s). Recreating fresh index.", exc)
            self._wipe_database_files()
            with self._session() as conn:
                self._apply_migrations(conn)

    def _wipe_database_files(self) -> None:
        for p in self.db_path.parent.glob("index.sqlite3*"):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    def _apply_migrations(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS repositories (
                id TEXT PRIMARY KEY,
                root_path TEXT NOT NULL,
                last_indexed_at TEXT NOT NULL,
                commit_sha TEXT
            );

            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                relative_path TEXT NOT NULL,
                language TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL,
                mtime REAL NOT NULL,
                is_test INTEGER NOT NULL DEFAULT 0,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                UNIQUE(repo_id, relative_path)
            );

            CREATE TABLE IF NOT EXISTS symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                line_number INTEGER NOT NULL,
                end_line_number INTEGER NOT NULL,
                parent_name TEXT,
                signature TEXT,
                docstring TEXT
            );

            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                module TEXT NOT NULL,
                imported_names TEXT NOT NULL,
                alias TEXT,
                line_number INTEGER NOT NULL,
                is_relative INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS references_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                symbol_name TEXT NOT NULL,
                line_number INTEGER NOT NULL,
                kind TEXT NOT NULL,
                target_path TEXT,
                target_symbol_name TEXT,
                is_resolved INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS dependencies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                ecosystem TEXT NOT NULL,
                name TEXT NOT NULL,
                version_constraint TEXT,
                scope TEXT NOT NULL,
                source_file TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS index_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                files_indexed INTEGER NOT NULL DEFAULT 0,
                files_removed INTEGER NOT NULL DEFAULT 0,
                duration_ms REAL NOT NULL DEFAULT 0.0,
                error_message TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_files_path ON files(repo_id, relative_path);
            CREATE INDEX IF NOT EXISTS idx_files_hash ON files(content_sha256);
            CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
            CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
            CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_id);
            CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(module);
            CREATE INDEX IF NOT EXISTS idx_refs_file ON references_index(file_id);
            CREATE INDEX IF NOT EXISTS idx_refs_symbol ON references_index(symbol_name);
            CREATE INDEX IF NOT EXISTS idx_deps_repo ON dependencies(repo_id);
            """
        )
        row = conn.execute("SELECT version FROM schema_migrations WHERE version = ?", (SCHEMA_VERSION,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, _now_iso()),
            )

    def _recover_interrupted_runs(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE index_runs SET status = 'interrupted', completed_at = ? WHERE status = 'running'",
            (_now_iso(),),
        )

    def get_repo_id(self) -> str:
        return str(self.workspace_root)

    def ensure_repository(self, conn: sqlite3.Connection, commit_sha: str | None = None) -> None:
        repo_id = self.get_repo_id()
        now = _now_iso()
        conn.execute(
            """
            INSERT INTO repositories (id, root_path, last_indexed_at, commit_sha)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_indexed_at = excluded.last_indexed_at,
                commit_sha = coalesce(excluded.commit_sha, repositories.commit_sha)
            """,
            (repo_id, str(self.workspace_root), now, commit_sha),
        )

    def start_run(self) -> int:
        repo_id = self.get_repo_id()
        now = _now_iso()
        with self._session() as conn:
            self.ensure_repository(conn)
            cur = conn.execute(
                "INSERT INTO index_runs (repo_id, started_at, status) VALUES (?, ?, 'running')",
                (repo_id, now),
            )
            return cur.lastrowid

    def finish_run(
        self,
        run_id: int,
        *,
        status: str = "completed",
        files_indexed: int = 0,
        files_removed: int = 0,
        duration_ms: float = 0.0,
        error_message: str | None = None,
    ) -> None:
        with self._session() as conn:
            conn.execute(
                """
                UPDATE index_runs
                SET completed_at = ?, status = ?, files_indexed = ?, files_removed = ?,
                    duration_ms = ?, error_message = ?
                WHERE id = ?
                """,
                (_now_iso(), status, files_indexed, files_removed, duration_ms, error_message, run_id),
            )

    def get_existing_files(self) -> dict[str, tuple[int, str, float]]:
        """Return relative_path -> (file_id, content_sha256, mtime)."""
        repo_id = self.get_repo_id()
        with self._session() as conn:
            rows = conn.execute(
                "SELECT id, relative_path, content_sha256, mtime FROM files WHERE repo_id = ? AND is_deleted = 0",
                (repo_id,),
            ).fetchall()
            return {row["relative_path"]: (row["id"], row["content_sha256"], row["mtime"]) for row in rows}

    def clear_index(self) -> None:
        repo_id = self.get_repo_id()
        with self._session() as conn:
            conn.execute("DELETE FROM files WHERE repo_id = ?", (repo_id,))
            conn.execute("DELETE FROM dependencies WHERE repo_id = ?", (repo_id,))

    def remove_files(self, paths: list[str]) -> None:
        if not paths:
            return
        repo_id = self.get_repo_id()
        with self._session() as conn:
            for p in paths:
                conn.execute("DELETE FROM files WHERE repo_id = ? AND relative_path = ?", (repo_id, p))

    def update_file_index(
        self,
        scanned: ScannedFile,
        extracted: ExtractedFileSymbols | None,
    ) -> None:
        repo_id = self.get_repo_id()
        with self._session() as conn:
            # Upsert file record
            cur = conn.execute(
                """
                INSERT INTO files (repo_id, relative_path, language, size_bytes, content_sha256, mtime, is_test, is_deleted)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(repo_id, relative_path) DO UPDATE SET
                    language = excluded.language,
                    size_bytes = excluded.size_bytes,
                    content_sha256 = excluded.content_sha256,
                    mtime = excluded.mtime,
                    is_test = excluded.is_test,
                    is_deleted = 0
                """,
                (
                    repo_id,
                    scanned.relative_path,
                    scanned.language,
                    scanned.size_bytes,
                    scanned.content_sha256,
                    scanned.mtime,
                    1 if scanned.is_test else 0,
                ),
            )
            # Get file id
            file_row = conn.execute(
                "SELECT id FROM files WHERE repo_id = ? AND relative_path = ?",
                (repo_id, scanned.relative_path),
            ).fetchone()
            if not file_row:
                return
            file_id = file_row["id"]

            # Remove previous symbols/imports/references for this file
            conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM imports WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM references_index WHERE file_id = ?", (file_id,))

            if extracted:
                # Insert symbols
                for sym in extracted.symbols:
                    conn.execute(
                        """
                        INSERT INTO symbols (file_id, name, kind, line_number, end_line_number, parent_name, signature, docstring)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            file_id,
                            sym.name,
                            sym.kind.value if isinstance(sym.kind, SymbolKind) else str(sym.kind),
                            sym.line_number,
                            sym.end_line_number,
                            sym.parent_name,
                            sym.signature,
                            sym.docstring,
                        ),
                    )

                # Insert imports
                for imp in extracted.imports:
                    conn.execute(
                        """
                        INSERT INTO imports (file_id, module, imported_names, alias, line_number, is_relative)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            file_id,
                            imp.module,
                            json.dumps(imp.imported_names),
                            imp.alias,
                            imp.line_number,
                            1 if imp.is_relative else 0,
                        ),
                    )

                # Insert references
                for ref in extracted.references:
                    conn.execute(
                        """
                        INSERT INTO references_index (file_id, symbol_name, line_number, kind, target_path, target_symbol_name, is_resolved)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            file_id,
                            ref.symbol_name,
                            ref.line_number,
                            ref.kind.value if isinstance(ref.kind, ReferenceKind) else str(ref.kind),
                            ref.target_path,
                            ref.target_symbol_name,
                            1 if ref.is_resolved else 0,
                        ),
                    )

    def record_dependencies(self, dependencies: list[DependencyRecord]) -> None:
        repo_id = self.get_repo_id()
        with self._session() as conn:
            conn.execute("DELETE FROM dependencies WHERE repo_id = ?", (repo_id,))
            for dep in dependencies:
                conn.execute(
                    """
                    INSERT INTO dependencies (repo_id, ecosystem, name, version_constraint, scope, source_file)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (repo_id, dep.ecosystem, dep.name, dep.version_constraint, dep.scope, dep.source_file),
                )

    def search_symbols(self, query: str, *, limit: int = 50) -> list[SearchSymbolResult]:
        repo_id = self.get_repo_id()
        pattern = f"%{query}%"
        with self._session() as conn:
            rows = conn.execute(
                """
                SELECT symbols.name, symbols.kind, files.relative_path, symbols.line_number,
                       symbols.end_line_number, symbols.parent_name, symbols.signature, symbols.docstring
                FROM symbols
                JOIN files ON files.id = symbols.file_id
                WHERE files.repo_id = ? AND files.is_deleted = 0 AND symbols.name LIKE ?
                ORDER BY
                    CASE WHEN symbols.name = ? THEN 1
                         WHEN symbols.name LIKE ? THEN 2
                         ELSE 3 END,
                    symbols.name ASC
                LIMIT ?
                """,
                (repo_id, pattern, query, f"{query}%", limit),
            ).fetchall()
            return [
                SearchSymbolResult(
                    name=r["name"],
                    kind=r["kind"],
                    path=r["relative_path"],
                    line_number=r["line_number"],
                    end_line_number=r["end_line_number"],
                    parent_name=r["parent_name"],
                    signature=r["signature"],
                    docstring=r["docstring"],
                )
                for r in rows
            ]

    def find_references(self, symbol_name: str, *, limit: int = 100) -> list[dict[str, Any]]:
        repo_id = self.get_repo_id()
        with self._session() as conn:
            rows = conn.execute(
                """
                SELECT references_index.symbol_name, references_index.line_number,
                       references_index.kind, references_index.target_path,
                       references_index.target_symbol_name, references_index.is_resolved,
                       files.relative_path AS source_path
                FROM references_index
                JOIN files ON files.id = references_index.file_id
                WHERE files.repo_id = ? AND files.is_deleted = 0 AND references_index.symbol_name = ?
                ORDER BY references_index.is_resolved DESC, files.relative_path ASC
                LIMIT ?
                """,
                (repo_id, symbol_name, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self) -> IndexStats:
        repo_id = self.get_repo_id()
        with self._session() as conn:
            total_files = conn.execute(
                "SELECT COUNT(*) FROM files WHERE repo_id = ? AND is_deleted = 0",
                (repo_id,),
            ).fetchone()[0]

            total_symbols = conn.execute(
                """
                SELECT COUNT(*) FROM symbols
                JOIN files ON files.id = symbols.file_id
                WHERE files.repo_id = ? AND files.is_deleted = 0
                """,
                (repo_id,),
            ).fetchone()[0]

            kind_rows = conn.execute(
                """
                SELECT symbols.kind, COUNT(*) AS count
                FROM symbols
                JOIN files ON files.id = symbols.file_id
                WHERE files.repo_id = ? AND files.is_deleted = 0
                GROUP BY symbols.kind
                ORDER BY count DESC
                """,
                (repo_id,),
            ).fetchall()
            symbols_by_kind = {r["kind"]: r["count"] for r in kind_rows}

            lang_rows = conn.execute(
                """
                SELECT language, COUNT(*) AS count
                FROM files
                WHERE repo_id = ? AND is_deleted = 0
                GROUP BY language
                ORDER BY count DESC
                """,
                (repo_id,),
            ).fetchall()
            languages = {r["language"]: r["count"] for r in lang_rows}

            last_run = conn.execute(
                """
                SELECT started_at, status, duration_ms
                FROM index_runs
                WHERE repo_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (repo_id,),
            ).fetchone()

            return IndexStats(
                total_files=total_files,
                total_symbols=total_symbols,
                symbols_by_kind=symbols_by_kind,
                languages=languages,
                last_run_at=last_run["started_at"] if last_run else None,
                last_run_status=last_run["status"] if last_run else None,
                last_run_duration_ms=last_run["duration_ms"] if last_run else 0.0,
            )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
