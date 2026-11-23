from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from forge.intelligence.retrieval import RetrievedContext, ScoredFileContext

logger = logging.getLogger("forge.intelligence.context")

DEFAULT_MAX_CONTEXT_TOKENS = 4000


def estimate_tokens(text: str) -> int:
    """Conservative token estimation fallback: ~4 characters per token rounded up."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


@dataclass(frozen=True)
class ContextItemManifest:
    path: str
    tokens: int
    reasons: list[str]
    score: float = 0.0


@dataclass(frozen=True)
class ContextManifest:
    budget_tokens: int
    used_tokens: int
    included_items: list[ContextItemManifest]
    excluded_items: list[ContextItemManifest]


@dataclass(frozen=True)
class BuiltContext:
    prompt_text: str
    manifest: ContextManifest


class TokenBudgetBuilder:
    """Builds token-budget-aware context with hard budget limits and explainable manifests."""

    def __init__(self, max_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS) -> None:
        if max_tokens <= 0:
            raise ValueError("Context budget max_tokens must be positive")
        self.max_tokens = max_tokens

    def build_context(self, retrieved: RetrievedContext) -> BuiltContext:
        remaining_budget = self.max_tokens
        included: list[ContextItemManifest] = []
        excluded: list[ContextItemManifest] = []
        sections: list[str] = []

        # 1. Project Instructions (Priority 1, up to 25% of budget)
        if retrieved.instructions:
            inst_budget = int(self.max_tokens * 0.25)
            inst_text = retrieved.instructions
            inst_tokens = estimate_tokens(inst_text)
            if inst_tokens > inst_budget:
                chars_limit = inst_budget * 4
                inst_text = inst_text[:chars_limit] + "\n... (truncated for budget)"
                inst_tokens = estimate_tokens(inst_text)

            sections.append(f"=== PROJECT INSTRUCTIONS ===\n{inst_text}")
            remaining_budget -= inst_tokens
            included.append(
                ContextItemManifest(
                    path="PROJECT_INSTRUCTIONS",
                    tokens=inst_tokens,
                    reasons=["Project guidelines and instructions"],
                )
            )

        # 2. Repository Map (Priority 2, up to 15% of budget)
        if retrieved.repo_map_summary and remaining_budget > 0:
            map_budget = min(remaining_budget, int(self.max_tokens * 0.15))
            map_text = retrieved.repo_map_summary
            map_tokens = estimate_tokens(map_text)
            if map_tokens > map_budget:
                chars_limit = map_budget * 4
                map_text = map_text[:chars_limit] + "\n..."
                map_tokens = estimate_tokens(map_text)

            sections.append(f"=== REPOSITORY MAP ===\n{map_text}")
            remaining_budget -= map_tokens
            included.append(
                ContextItemManifest(
                    path="REPOSITORY_MAP",
                    tokens=map_tokens,
                    reasons=["Structural overview and framework summary"],
                )
            )

        # 3. Source and Test Files by score priority
        impl_files = [f for f in retrieved.scored_files if not f.is_test]
        test_files = [f for f in retrieved.scored_files if f.is_test]

        # Prioritize implementation files first, then tests
        candidate_queue = impl_files + test_files

        for file_ctx in candidate_queue:
            file_tokens = estimate_tokens(file_ctx.content)
            reasons_str = ", ".join(file_ctx.reasons)

            if remaining_budget <= 50:
                excluded.append(
                    ContextItemManifest(
                        path=file_ctx.relative_path,
                        tokens=file_tokens,
                        reasons=["Budget ceiling reached; omitted"],
                        score=file_ctx.score,
                    )
                )
                continue

            # If file fits entirely in remaining budget
            if file_tokens <= remaining_budget:
                sections.append(
                    f"=== FILE: {file_ctx.relative_path} (Relevance: {file_ctx.score:g} | {reasons_str}) ===\n"
                    f"{file_ctx.content}"
                )
                remaining_budget -= file_tokens
                included.append(
                    ContextItemManifest(
                        path=file_ctx.relative_path,
                        tokens=file_tokens,
                        reasons=file_ctx.reasons,
                        score=file_ctx.score,
                    )
                )
            else:
                # File is partially included if we have at least 150 tokens
                if remaining_budget >= 150:
                    take_chars = (remaining_budget - 20) * 4
                    truncated_content = file_ctx.content[:take_chars] + f"\n... (truncated: {len(file_ctx.content)} total chars)"
                    part_tokens = estimate_tokens(truncated_content)
                    sections.append(
                        f"=== FILE: {file_ctx.relative_path} (Relevance: {file_ctx.score:g} | {reasons_str} | Truncated) ===\n"
                        f"{truncated_content}"
                    )
                    included.append(
                        ContextItemManifest(
                            path=file_ctx.relative_path,
                            tokens=part_tokens,
                            reasons=file_ctx.reasons + ["Truncated to fit remaining context budget"],
                            score=file_ctx.score,
                        )
                    )
                    remaining_budget -= part_tokens
                else:
                    excluded.append(
                        ContextItemManifest(
                            path=file_ctx.relative_path,
                            tokens=file_tokens,
                            reasons=["Insufficient remaining token budget"],
                            score=file_ctx.score,
                        )
                    )

        prompt_text = "\n\n".join(sections)
        used_tokens = self.max_tokens - remaining_budget

        manifest = ContextManifest(
            budget_tokens=self.max_tokens,
            used_tokens=used_tokens,
            included_items=included,
            excluded_items=excluded,
        )

        return BuiltContext(prompt_text=prompt_text, manifest=manifest)
