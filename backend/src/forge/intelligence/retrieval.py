from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.context.workspace import Workspace
from forge.intelligence.scanner import RepoScanner, is_test_file
from forge.intelligence.store import IndexStore

STOP_WORDS = {
    "a", "an", "the", "in", "on", "at", "to", "for", "with", "from",
    "by", "and", "or", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "this", "that", "these",
    "those", "it", "its", "of", "as", "if", "into", "we", "you", "they",
    "can", "could", "should", "would", "will", "please", "make", "update",
    "add", "fix", "implement", "check", "run", "test", "write", "code",
}


@dataclass(frozen=True)
class ScoredFileContext:
    relative_path: str
    score: float
    reasons: list[str]
    content: str
    is_test: bool = False


@dataclass(frozen=True)
class RetrievedContext:
    task: str
    instructions: str | None
    repo_map_summary: str
    scored_files: list[ScoredFileContext]
    git_modified_files: list[str] = field(default_factory=list)


class ContextRetrievalEngine:
    """Task-oriented context retrieval engine with explainable scoring and deduplication."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        store: IndexStore | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.store = store or IndexStore(self.workspace_root)
        self.workspace = workspace or Workspace(self.workspace_root)
        self.scanner = RepoScanner(self.workspace_root, workspace=self.workspace)

    def extract_keywords(self, task_text: str) -> list[str]:
        tokens = re.findall(r"[a-zA-Z0-9_\-\.]+", task_text)
        keywords: list[str] = []
        for t in tokens:
            t_clean = t.strip("._-").lower()
            if len(t_clean) >= 2 and t_clean not in STOP_WORDS:
                keywords.append(t_clean)
            # If camelCase or PascalCase, split
            parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\b)", t)
            for part in parts:
                p_clean = part.lower()
                if len(p_clean) >= 2 and p_clean not in STOP_WORDS:
                    keywords.append(p_clean)
        return sorted(set(keywords))

    def retrieve(
        self,
        task_text: str,
        *,
        candidate_paths: list[str] | None = None,
        max_source_files: int = 10,
    ) -> RetrievedContext:
        """Score repository files based on task keywords, symbols, tests, imports, and git status."""
        keywords = self.extract_keywords(task_text)
        file_scores: dict[str, float] = defaultdict(float)
        file_reasons: dict[str, list[str]] = defaultdict(list)

        inventory = self.scanner.scan()
        repo_map = self.scanner.generate_repo_map(inventory)

        # 1. Project instructions
        discovered_ctx = self.workspace.discover_context(file_limit=100)
        instructions = discovered_ctx.instructions

        # 2. Check Git modified files (boost relevance)
        git_modified: list[str] = []
        if inventory.git_meta and inventory.git_meta.modified_files:
            git_modified = inventory.git_meta.modified_files
            for g_path in git_modified:
                file_scores[g_path] += 40.0
                file_reasons[g_path].append("Currently modified in Git")

        # 3. Explicitly requested candidate paths
        if candidate_paths:
            for c_path in candidate_paths:
                file_scores[c_path] += 50.0
                file_reasons[c_path].append("Explicitly specified in task context")

        # 4. Search symbols matching keywords via SQLite index
        matched_symbols_by_path: dict[str, list[str]] = defaultdict(list)
        for kw in keywords:
            if len(kw) < 3:
                continue
            symbol_hits = self.store.search_symbols(kw, limit=10)
            for sym in symbol_hits:
                matched_symbols_by_path[sym.path].append(sym.name)
                # Exact vs partial match
                if sym.name.lower() == kw:
                    file_scores[sym.path] += 30.0
                    file_reasons[sym.path].append(f"Exact symbol match: {sym.name}")
                else:
                    file_scores[sym.path] += 15.0
                    file_reasons[sym.path].append(f"Symbol match: {sym.name}")

                # Find references to this symbol in other files
                refs = self.store.find_references(sym.name, limit=5)
                for ref in refs:
                    ref_source = ref["source_path"]
                    if ref_source != sym.path:
                        file_scores[ref_source] += 10.0
                        file_reasons[ref_source].append(f"References symbol {sym.name}")

        # 5. Path match with keywords
        for f in inventory.files:
            rel_path = f.relative_path
            path_lower = rel_path.lower()
            for kw in keywords:
                if kw in path_lower:
                    file_scores[rel_path] += 15.0
                    file_reasons[rel_path].append(f"Path matches keyword: {kw}")

        # 6. Find related test files for scored implementation files
        scored_impl_files = [p for p in file_scores.keys() if not is_test_file(p)]
        for impl_path in scored_impl_files:
            impl_name = Path(impl_path).stem
            # Look for matching test files in inventory
            for f in inventory.files:
                if f.is_test:
                    test_p = f.relative_path
                    if impl_name in test_p.lower():
                        file_scores[test_p] += 25.0
                        file_reasons[test_p].append(f"Associated test file for {impl_path}")

        # Fallback for generic or non-keyword tasks: include top entry-point files
        if not file_scores and inventory.files:
            ENTRY_NAMES = {
                "main.py", "app.py", "index.ts", "index.js", "readme.md",
                "pyproject.toml", "package.json",
            }
            for f in inventory.files:
                name_lower = Path(f.relative_path).name.lower()
                if name_lower in ENTRY_NAMES:
                    file_scores[f.relative_path] += 20.0
                    file_reasons[f.relative_path].append("Repository entry point")
            if not file_scores:
                for f in inventory.files[:5]:
                    file_scores[f.relative_path] += 10.0
                    file_reasons[f.relative_path].append("Workspace file")

        # Rank and select top files
        ranked_paths = sorted(
            file_scores.keys(),
            key=lambda p: (-file_scores[p], p),
        )

        scored_contexts: list[ScoredFileContext] = []
        for path_str in ranked_paths:
            if len(scored_contexts) >= max_source_files:
                break
            try:
                content = self.workspace.read_text(path_str)
                # Deduplicate reasons while preserving order
                unique_reasons = list(dict.fromkeys(file_reasons[path_str]))
                scored_contexts.append(
                    ScoredFileContext(
                        relative_path=path_str,
                        score=file_scores[path_str],
                        reasons=unique_reasons,
                        content=content,
                        is_test=is_test_file(path_str),
                    )
                )
            except Exception:
                continue

        repo_map_combined = f"{repo_map.summary_text}\n{repo_map.tree_text}" if repo_map.tree_text else repo_map.summary_text

        return RetrievedContext(
            task=task_text,
            instructions=instructions,
            repo_map_summary=repo_map_combined,
            scored_files=scored_contexts,
            git_modified_files=git_modified,
        )
