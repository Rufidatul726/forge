from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from forge.agent import AgentService, AgentTaskRequest
from forge.context import Workspace
from forge.intelligence.context import TokenBudgetBuilder, estimate_tokens
from forge.intelligence.indexer import RepositoryIndexer
from forge.intelligence.models import ReferenceRecord
from forge.intelligence.scanner import RepoScanner
from forge.intelligence.store import IndexStore
from forge.providers.models import ChatMessage, GenerationResult


def run(coro):
    return asyncio.run(coro)


# 1. Truncated scans do not incorrectly delete un-scanned files
def test_truncated_scan_does_not_delete_unseen_files(tmp_path: Path):
    # Create 8 files
    for i in range(8):
        (tmp_path / f"file_{i}.py").write_text(f"x_{i} = {i}\n", encoding="utf-8")

    store = IndexStore(tmp_path)
    # Complete initial index
    indexer = RepositoryIndexer(tmp_path, store=store)
    stats1 = indexer.index()
    assert stats1.total_files == 8
    existing_before = store.get_existing_files()
    assert len(existing_before) == 8

    # Now simulate a scanner with max_files = 3 (causing inventory.truncated = True)
    limited_scanner = RepoScanner(tmp_path, max_files=3)
    inv = limited_scanner.scan()
    assert inv.truncated is True
    assert len(inv.files) == 3

    # Run indexer using this truncated inventory
    indexer.scanner = limited_scanner
    stats2 = indexer.index()

    # The 5 unseen files must NOT be removed from SQLite index
    existing_after = store.get_existing_files()
    assert len(existing_after) == 8
    assert stats2.total_files == 8


# 2. Dependency cleanup when manifest is removed
def test_dependency_cleanup_when_manifest_removed(tmp_path: Path):
    # Initially create pyproject.toml with dependencies
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "demo"
dependencies = [
    "httpx>=0.28.0",
    "pydantic>=2.0.0",
]
""",
        encoding="utf-8",
    )

    store = IndexStore(tmp_path)
    indexer = RepositoryIndexer(tmp_path, store=store)
    indexer.index()

    # Verify dependencies are in store
    with store._session() as conn:
        deps = conn.execute("SELECT name FROM dependencies").fetchall()
    dep_names = {row[0] for row in deps}
    assert dep_names == {"httpx", "pydantic"}

    # Remove the manifest file entirely
    pyproject.unlink()

    # Re-index with a complete scan
    indexer.index()

    # The dependency snapshot must now be empty
    with store._session() as conn:
        deps_after = conn.execute("SELECT name FROM dependencies").fetchall()
    assert len(deps_after) == 0


# 3. Oversized-file hashes cover bytes beyond the first 1 MiB
def test_oversized_file_hash_covers_bytes_beyond_first_mib(tmp_path: Path):
    scanner = RepoScanner(tmp_path)
    large_file = tmp_path / "large_data.bin"

    # Create 1.5 MiB file: 1 MiB of 'A' + 0.5 MiB of 'B'
    one_mib = 1024 * 1024
    half_mib = 512 * 1024
    content_v1 = b"A" * one_mib + b"B" * half_mib
    large_file.write_bytes(content_v1)

    hash_v1 = scanner._compute_file_sha256(large_file, len(content_v1))
    assert hash_v1 != ""

    # Modify ONLY bytes in the second half (beyond 1 MiB)
    content_v2 = b"A" * one_mib + b"C" * half_mib
    large_file.write_bytes(content_v2)

    hash_v2 = scanner._compute_file_sha256(large_file, len(content_v2))
    assert hash_v2 != ""

    # The streaming full-file hash must detect the change beyond 1 MiB
    assert hash_v1 != hash_v2

    # Verify incremental indexer detects the update
    store = IndexStore(tmp_path)
    indexer = RepositoryIndexer(tmp_path, store=store)
    stats1 = indexer.index()
    assert stats1.total_files == 1

    existing1 = store.get_existing_files()
    assert existing1["large_data.bin"][1] == hash_v2

    # Modify byte at offset 1.2 MiB
    modified_bytes = bytearray(content_v2)
    modified_bytes[one_mib + 200_000] = ord(b"Z")
    large_file.write_bytes(bytes(modified_bytes))
    hash_v3 = scanner._compute_file_sha256(large_file, len(modified_bytes))
    assert hash_v3 not in (hash_v1, hash_v2)

    stats2 = indexer.index()
    assert stats2.total_files == 1

    existing2 = store.get_existing_files()
    # Must be re-indexed with new content hash because hash covers entire file
    assert existing2["large_data.bin"][1] == hash_v3


# 4. Agent context pipeline includes relevant test files within budget
def test_agent_context_pipeline_includes_relevant_tests_within_budget(tmp_path: Path):
    (tmp_path / "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (tmp_path / "test_calculator.py").write_text(
        "def test_add():\n    assert add(1, 2) == 3\n\ndef test_subtract():\n    assert subtract(5, 2) == 3\n",
        encoding="utf-8",
    )

    class FakeProvider:
        async def generate(self, payload):
            # Verify system message contains the relevant test file
            sys_msg = payload.messages[0].content
            assert "test_calculator.py" in sys_msg
            assert "calculator.py" in sys_msg
            return GenerationResult(model=payload.model, content="Analysis of add and tests complete.", done=True)

    svc = AgentService(tmp_path, FakeProvider())
    task = AgentTaskRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="Review calculator logic and its associated tests")],
    )

    result = run(
        svc.run_autonomous(
            task,
            approve_write=lambda p: True,
            approve_command=lambda p: True,
        )
    )

    # Relevant test file must be retrieved and included in context
    assert "test_calculator.py" in result.context_paths
    assert result.context_manifest is not None
    included_manifest_paths = {item.path for item in result.context_manifest.included_items}
    assert "test_calculator.py" in included_manifest_paths
    test_item = next(item for item in result.context_manifest.included_items if item.path == "test_calculator.py")
    assert test_item.is_test is True


# 5. Strict budget enforcement against complete assembled prompt
def test_strict_budget_enforcement_against_complete_assembled_prompt():
    from forge.intelligence.retrieval import RetrievedContext, ScoredFileContext

    builder = TokenBudgetBuilder(max_tokens=250)
    huge_source = "def logic():\n    return 42\n" * 200
    huge_test = "def test_logic():\n    assert logic() == 42\n" * 200

    retrieved = RetrievedContext(
        task="inspect logic and tests",
        instructions="Standard guidelines for coding and safety." * 30,
        repo_map_summary="Map summary overview of repository components." * 20,
        scored_files=[
            ScoredFileContext("src/logic.py", 90.0, ["Exact symbol match"], huge_source, is_test=False),
            ScoredFileContext("tests/test_logic.py", 85.0, ["Associated test file"], huge_test, is_test=True),
        ],
    )

    built = builder.build_context(retrieved)

    # Strict token budget enforcement with zero-token tolerance
    assert built.manifest.used_tokens <= 250
    assert estimate_tokens(built.prompt_text) <= 250
    assert built.manifest.used_tokens == estimate_tokens(built.prompt_text)
    # Both implementation file and test file are accounted for in manifest
    all_paths = {item.path for item in built.manifest.included_items} | {item.path for item in built.manifest.excluded_items}
    assert "src/logic.py" in all_paths
    assert "tests/test_logic.py" in all_paths


# 6. Gitignore support: negation rules, nested files, anchored and directory patterns
def test_gitignore_negation_nested_and_directory_semantics(tmp_path: Path):
    # Root .gitignore with negation, anchored rule, and directory-only rule
    (tmp_path / ".gitignore").write_text(
        """
# Top-level ignores
*.log
!keep.log
/root_only.txt
temp_dir/
""",
        encoding="utf-8",
    )

    # Subdirectory with nested .gitignore
    sub_dir = tmp_path / "sub"
    sub_dir.mkdir()
    (sub_dir / ".gitignore").write_text(
        """
*.sub
!keep.sub
""",
        encoding="utf-8",
    )

    # Create matching and non-matching files
    (tmp_path / "ignored.log").write_text("ignore", encoding="utf-8")
    (tmp_path / "keep.log").write_text("keep me", encoding="utf-8")
    (tmp_path / "root_only.txt").write_text("root ignored", encoding="utf-8")
    (sub_dir / "root_only.txt").write_text("sub not ignored", encoding="utf-8")

    temp_dir = tmp_path / "temp_dir"
    temp_dir.mkdir()
    (temp_dir / "data.txt").write_text("inside temp_dir", encoding="utf-8")

    (sub_dir / "ignored.sub").write_text("sub ignored", encoding="utf-8")
    (sub_dir / "keep.sub").write_text("sub keep me", encoding="utf-8")
    (tmp_path / "main.py").write_text("print('hello')", encoding="utf-8")

    ws = Workspace(tmp_path)
    file_list = ws.list_files().paths

    # Verify root negation
    assert "keep.log" in file_list
    assert "ignored.log" not in file_list

    # Verify anchored root rule
    assert "root_only.txt" not in file_list
    assert "sub/root_only.txt" in file_list

    # Verify directory-only rule
    assert "temp_dir/data.txt" not in file_list

    # Verify nested .gitignore with negation
    assert "sub/keep.sub" in file_list
    assert "sub/ignored.sub" not in file_list

    assert "main.py" in file_list


# 7. Reference resolution distinction documented
def test_reference_resolution_distinction_explicit():
    rec = ReferenceRecord(
        source_path="app.py",
        symbol_name="helper",
        line_number=10,
        is_resolved=False,
    )
    assert rec.is_resolved is False
    assert "Approximate syntactic reference" in (ReferenceRecord.__doc__ or "")
