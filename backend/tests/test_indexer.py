import sqlite3
from pathlib import Path

import pytest

from forge.intelligence.indexer import RepositoryIndexer
from forge.intelligence.models import SymbolKind
from forge.intelligence.store import IndexStore


def test_indexer_initial_run(tmp_path: Path):
    (tmp_path / "main.py").write_text("def hello(): return 'world'\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text("export class Service { run() {} }\n", encoding="utf-8")

    indexer = RepositoryIndexer(tmp_path)
    stats = indexer.index()

    assert stats.total_files == 2
    assert stats.total_symbols >= 2
    assert stats.last_run_status == "completed"

    store = IndexStore(tmp_path)
    matches = store.search_symbols("hello")
    assert len(matches) == 1
    assert matches[0].name == "hello"
    assert matches[0].path == "main.py"

    ts_matches = store.search_symbols("Service")
    assert len(ts_matches) == 1
    assert ts_matches[0].name == "Service"
    assert ts_matches[0].path == "app.ts"


def test_indexer_incremental_updates(tmp_path: Path):
    file1 = tmp_path / "calc.py"
    file1.write_text("def add(a, b): return a + b\n", encoding="utf-8")

    indexer = RepositoryIndexer(tmp_path)
    indexer.index()

    store = IndexStore(tmp_path)
    assert len(store.search_symbols("add")) == 1

    # 1. Modify existing file
    file1.write_text("def multiply(a, b): return a * b\n", encoding="utf-8")
    indexer.index()

    # 'add' should no longer exist, 'multiply' should exist
    assert len(store.search_symbols("add")) == 0
    assert len(store.search_symbols("multiply")) == 1

    # 2. Add a new file
    file2 = tmp_path / "extra.py"
    file2.write_text("def extra_func(): pass\n", encoding="utf-8")
    indexer.index()

    assert len(store.search_symbols("extra_func")) == 1
    assert len(store.search_symbols("multiply")) == 1
    assert store.get_stats().total_files == 2

    # 3. Delete a file
    file2.unlink()
    indexer.index()

    assert len(store.search_symbols("extra_func")) == 0
    assert len(store.search_symbols("multiply")) == 1
    assert store.get_stats().total_files == 1


def test_indexer_corrupted_db_recovery(tmp_path: Path):
    (tmp_path / "main.py").write_text("def initial(): pass\n", encoding="utf-8")
    indexer = RepositoryIndexer(tmp_path)
    indexer.index()

    # Remove WAL files and corrupt the main index sqlite3 file with random junk
    for p in (tmp_path / ".forge").glob("index.sqlite3-*"):
        p.unlink(missing_ok=True)
    db_file = tmp_path / ".forge" / "index.sqlite3"
    db_file.write_bytes(b"JUNK_DATA_CORRUPT_SQLITE_HEADER")

    # Creating IndexStore should recover cleanly
    recovered_store = IndexStore(tmp_path)
    assert recovered_store.get_stats().total_files == 0

    # Re-indexing should succeed without error
    indexer2 = RepositoryIndexer(tmp_path, store=recovered_store)
    stats = indexer2.index()
    assert stats.total_files == 1
    assert stats.last_run_status == "completed"


def test_indexer_interrupted_run_recovery(tmp_path: Path):
    (tmp_path / "main.py").write_text("def test(): pass\n", encoding="utf-8")
    store = IndexStore(tmp_path)

    # Start a run but never finish it
    run_id = store.start_run()

    # Verify run is marked 'running'
    with store._connect() as conn:
        status = conn.execute("SELECT status FROM index_runs WHERE id = ?", (run_id,)).fetchone()[0]
        assert status == "running"

    # Instantiate a new store (as on process restart)
    store2 = IndexStore(tmp_path)
    with store2._connect() as conn:
        status2 = conn.execute("SELECT status FROM index_runs WHERE id = ?", (run_id,)).fetchone()[0]
        assert status2 == "interrupted"
