from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from forge.config import forge_directory
from forge.providers.models import ChatMessage


class SessionError(Exception):
    """Base error for session persistence."""


class SessionNotFoundError(SessionError):
    """The requested session does not exist."""


@dataclass(frozen=True)
class SessionSummary:
    id: str
    title: str
    created_at: str
    updated_at: str
    turn_count: int


@dataclass(frozen=True)
class StoredTurn:
    role: str
    content: str
    created_at: str


@dataclass(frozen=True)
class StoredOutcome:
    kind: str
    payload: dict[str, object]
    created_at: str


@dataclass(frozen=True)
class Session:
    summary: SessionSummary
    turns: list[StoredTurn]
    outcomes: list[StoredOutcome]


class SessionStore:
    """SQLite-backed sessions scoped to one explicit workspace."""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root.expanduser().resolve(strict=True)
        if not self._workspace_root.is_dir():
            raise ValueError("Session workspace root must be a directory")
        self._database_path = forge_directory(self._workspace_root) / "sessions.sqlite3"
        self._initialize()

    def create(self, title: str = "New session") -> SessionSummary:
        normalized_title = title.strip()
        if not normalized_title or len(normalized_title) > 120:
            raise ValueError("Session title must contain between 1 and 120 characters")
        session_id = uuid4().hex
        now = _timestamp()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, normalized_title, now, now),
            )
        return SessionSummary(session_id, normalized_title, now, now, 0)

    def list(self, *, limit: int = 100) -> list[SessionSummary]:
        if not 1 <= limit <= 100:
            raise ValueError("Session list limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sessions.id, sessions.title, sessions.created_at, sessions.updated_at,
                       COUNT(turns.id) AS turn_count
                FROM sessions
                LEFT JOIN turns ON turns.session_id = sessions.id
                GROUP BY sessions.id
                ORDER BY sessions.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [SessionSummary(**dict(row)) for row in rows]

    def get(self, session_id: str) -> Session:
        with self._connect() as connection:
            summary = self._summary(connection, session_id)
            turns = [
                StoredTurn(**dict(row))
                for row in connection.execute(
                    "SELECT role, content, created_at FROM turns WHERE session_id = ? ORDER BY id",
                    (session_id,),
                )
            ]
            outcomes = [
                StoredOutcome(
                    kind=row["kind"],
                    payload=json.loads(row["payload"]),
                    created_at=row["created_at"],
                )
                for row in connection.execute(
                    "SELECT kind, payload, created_at FROM outcomes WHERE session_id = ? ORDER BY id",
                    (session_id,),
                )
            ]
        return Session(summary=summary, turns=turns, outcomes=outcomes)

    def append_turns(self, session_id: str, messages: list[ChatMessage]) -> None:
        if not messages:
            return
        now = _timestamp()
        with self._connect() as connection:
            self._summary(connection, session_id)
            connection.executemany(
                "INSERT INTO turns (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                [(session_id, message.role, message.content, now) for message in messages],
            )
            self._touch(connection, session_id, now)

    def append_outcome(self, session_id: str, kind: str, payload: dict[str, object]) -> None:
        if not kind or len(kind) > 80:
            raise ValueError("Outcome kind must contain between 1 and 80 characters")
        encoded_payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        now = _timestamp()
        with self._connect() as connection:
            self._summary(connection, session_id)
            connection.execute(
                "INSERT INTO outcomes (session_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (session_id, kind, encoded_payload, now),
            )
            self._touch(connection, session_id, now)

    def delete(self, session_id: str) -> None:
        with self._connect() as connection:
            result = connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            if result.rowcount != 1:
                raise SessionNotFoundError("Session does not exist")

    def _initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO schema_migrations (version) VALUES (1);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _summary(connection: sqlite3.Connection, session_id: str) -> SessionSummary:
        row = connection.execute(
            """
            SELECT sessions.id, sessions.title, sessions.created_at, sessions.updated_at,
                   COUNT(turns.id) AS turn_count
            FROM sessions
            LEFT JOIN turns ON turns.session_id = sessions.id
            WHERE sessions.id = ?
            GROUP BY sessions.id
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise SessionNotFoundError("Session does not exist")
        return SessionSummary(**dict(row))

    @staticmethod
    def _touch(connection: sqlite3.Connection, session_id: str, timestamp: str) -> None:
        connection.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (timestamp, session_id))


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()
