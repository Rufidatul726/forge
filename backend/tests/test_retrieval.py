from pathlib import Path

from forge.intelligence.context import TokenBudgetBuilder, estimate_tokens
from forge.intelligence.indexer import RepositoryIndexer
from forge.intelligence.retrieval import ContextRetrievalEngine


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") == 3  # (11 + 3) // 4 = 3
    assert estimate_tokens("a" * 400) == 100


def test_context_retrieval_and_budget(tmp_path: Path):
    # Setup test workspace
    (tmp_path / "FORGE.md").write_text("# Project Guidelines\nAlways write clean code.\n", encoding="utf-8")
    (tmp_path / "auth.py").write_text("class Authenticator:\n    def login(self, username, password): return True\n", encoding="utf-8")
    (tmp_path / "test_auth.py").write_text("from auth import Authenticator\ndef test_login(): pass\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("def unrelated_task(): pass\n", encoding="utf-8")

    # Index the repository
    indexer = RepositoryIndexer(tmp_path)
    indexer.index()

    # Query for auth task
    retrieval = ContextRetrievalEngine(tmp_path)
    retrieved = retrieval.retrieve("Fix login bug in Authenticator and verify tests")

    assert len(retrieved.scored_files) >= 2
    scored_paths = [f.relative_path for f in retrieved.scored_files]

    # auth.py should be top-ranked due to Authenticator and login symbol matches
    assert "auth.py" in scored_paths
    assert "test_auth.py" in scored_paths

    auth_ctx = next(f for f in retrieved.scored_files if f.relative_path == "auth.py")
    assert any("Authenticator" in r or "login" in r for r in auth_ctx.reasons)

    test_ctx = next(f for f in retrieved.scored_files if f.relative_path == "test_auth.py")
    assert any("Associated test" in r or "test" in r.lower() for r in test_ctx.reasons)

    # Build context within strict budget
    builder = TokenBudgetBuilder(max_tokens=500)
    built = builder.build_context(retrieved)

    # Budget must be strictly respected
    assert built.manifest.used_tokens <= 500
    assert "PROJECT INSTRUCTIONS" in built.prompt_text
    assert "auth.py" in built.prompt_text

    # Manifest should list included items
    included_names = [item.path for item in built.manifest.included_items]
    assert "auth.py" in included_names
    assert "PROJECT_INSTRUCTIONS" in included_names


def test_token_budget_hard_limit_truncation_and_exclusion(tmp_path: Path):
    (tmp_path / "large_file.py").write_text("def large(): pass\n" + "# padding line\n" * 200, encoding="utf-8")
    (tmp_path / "second_file.py").write_text("def second(): pass\n", encoding="utf-8")

    indexer = RepositoryIndexer(tmp_path)
    indexer.index()

    retrieval = ContextRetrievalEngine(tmp_path)
    retrieved = retrieval.retrieve("inspect large_file and second_file")

    # Very small budget: 100 tokens
    builder = TokenBudgetBuilder(max_tokens=100)
    built = builder.build_context(retrieved)

    assert built.manifest.used_tokens <= 100
    # Either large_file was truncated or excluded to satisfy hard limit
    assert len(built.manifest.excluded_items) > 0 or any("Truncated" in r for item in built.manifest.included_items for r in item.reasons)
