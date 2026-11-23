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
    is_test: bool = False


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
        included: list[ContextItemManifest] = []
        excluded: list[ContextItemManifest] = []
        sections: list[str] = []

        def current_tokens_with(tentative_section: str | None = None) -> int:
            if tentative_section is None:
                if not sections:
                    return 0
                return estimate_tokens("\n\n".join(sections))
            if not sections:
                return estimate_tokens(tentative_section)
            return estimate_tokens("\n\n".join([*sections, tentative_section]))

        # 1. Project Instructions (Priority 1, up to 25% of total budget)
        if retrieved.instructions:
            inst_budget = max(40, int(self.max_tokens * 0.25))
            inst_header = "=== PROJECT INSTRUCTIONS ===\nProject instructions:\n"
            inst_suffix = "\n... (truncated for budget)"
            full_inst_sec = f"{inst_header}{retrieved.instructions}"

            if estimate_tokens(full_inst_sec) <= inst_budget:
                inst_sec = full_inst_sec
            else:
                # Find maximum characters that fit inside inst_budget
                low = 0
                high = len(retrieved.instructions)
                best_inst = ""
                while low <= high:
                    mid = (low + high) // 2
                    cand = f"{inst_header}{retrieved.instructions[:mid]}{inst_suffix}"
                    if estimate_tokens(cand) <= inst_budget:
                        best_inst = cand
                        low = mid + 1
                    else:
                        high = mid - 1
                inst_sec = best_inst or f"{inst_header}{inst_suffix}"

            if current_tokens_with(inst_sec) <= self.max_tokens:
                sections.append(inst_sec)
                inst_tokens = estimate_tokens(inst_sec)
                included.append(
                    ContextItemManifest(
                        path="PROJECT_INSTRUCTIONS",
                        tokens=inst_tokens,
                        reasons=["Project guidelines and instructions"],
                        is_test=False,
                    )
                )

        # 2. Repository Map (Priority 2, up to 15% of total budget)
        curr_tok = current_tokens_with()
        if retrieved.repo_map_summary and curr_tok < self.max_tokens:
            map_budget = max(20, min(self.max_tokens - curr_tok, int(self.max_tokens * 0.15)))
            map_header = "=== REPOSITORY MAP ===\n"
            map_suffix = "\n..."
            full_map_sec = f"{map_header}{retrieved.repo_map_summary}"

            if estimate_tokens(full_map_sec) <= map_budget:
                map_sec = full_map_sec
            else:
                low = 0
                high = len(retrieved.repo_map_summary)
                best_map = ""
                while low <= high:
                    mid = (low + high) // 2
                    cand = f"{map_header}{retrieved.repo_map_summary[:mid]}{map_suffix}"
                    if estimate_tokens(cand) <= map_budget:
                        best_map = cand
                        low = mid + 1
                    else:
                        high = mid - 1
                map_sec = best_map or f"{map_header}{map_suffix}"

            if current_tokens_with(map_sec) <= self.max_tokens:
                sections.append(map_sec)
                map_tokens = estimate_tokens(map_sec)
                included.append(
                    ContextItemManifest(
                        path="REPOSITORY_MAP",
                        tokens=map_tokens,
                        reasons=["Structural overview and framework summary"],
                        is_test=False,
                    )
                )

        # 3. Source and Test Files ordered strictly by relevance score
        # Both implementation files and test files are included according to their scores and reasons
        candidate_queue = sorted(
            retrieved.scored_files,
            key=lambda f: (-f.score, f.relative_path),
        )

        candidate_count = len(candidate_queue)
        for idx, file_ctx in enumerate(candidate_queue):
            reasons_str = ", ".join(file_ctx.reasons)
            file_header = f"=== FILE: {file_ctx.relative_path} (Relevance: {file_ctx.score:g} | {reasons_str}) ===\nFile: {file_ctx.relative_path}\n"
            full_file_sec = f"{file_header}{file_ctx.content}"
            curr_used = current_tokens_with()
            avail = self.max_tokens - curr_used

            if avail <= 20:
                excluded.append(
                    ContextItemManifest(
                        path=file_ctx.relative_path,
                        tokens=estimate_tokens(file_ctx.content),
                        reasons=["Budget ceiling reached; omitted"],
                        score=file_ctx.score,
                        is_test=file_ctx.is_test,
                    )
                )
                continue

            # Fair-share budget allocation: prevent earlier files from starving subsequent files
            remaining_files = candidate_count - idx
            fair_share = max(120, avail // remaining_files)
            budget_limit_for_file = min(self.max_tokens, curr_used + fair_share)

            # If full section fits in total assembled prompt budget and within fair share
            if current_tokens_with(full_file_sec) <= budget_limit_for_file:
                sections.append(full_file_sec)
                item_tok = current_tokens_with() - curr_used
                included.append(
                    ContextItemManifest(
                        path=file_ctx.relative_path,
                        tokens=item_tok,
                        reasons=file_ctx.reasons,
                        score=file_ctx.score,
                        is_test=file_ctx.is_test,
                    )
                )
            else:
                # Partial inclusion if sufficient room remains for header + content snippet
                trunc_header = f"=== FILE: {file_ctx.relative_path} (Relevance: {file_ctx.score:g} | {reasons_str} | Truncated) ===\nFile: {file_ctx.relative_path}\n"
                trunc_suffix = f"\n... (truncated: {len(file_ctx.content)} total chars)"
                min_cand = f"{trunc_header}{trunc_suffix}"

                if current_tokens_with(min_cand) + 10 <= budget_limit_for_file:
                    low = 0
                    high = len(file_ctx.content)
                    best_sec = None
                    while low <= high:
                        mid = (low + high) // 2
                        cand_content = file_ctx.content[:mid]
                        cand_sec = f"{trunc_header}{cand_content}{trunc_suffix}"
                        if current_tokens_with(cand_sec) <= budget_limit_for_file:
                            best_sec = cand_sec
                            low = mid + 1
                        else:
                            high = mid - 1

                    if best_sec is not None:
                        sections.append(best_sec)
                        item_tok = current_tokens_with() - curr_used
                        included.append(
                            ContextItemManifest(
                                path=file_ctx.relative_path,
                                tokens=item_tok,
                                reasons=file_ctx.reasons + ["Truncated to fit remaining context budget"],
                                score=file_ctx.score,
                                is_test=file_ctx.is_test,
                            )
                        )
                    else:
                        excluded.append(
                            ContextItemManifest(
                                path=file_ctx.relative_path,
                                tokens=estimate_tokens(file_ctx.content),
                                reasons=["Insufficient remaining token budget"],
                                score=file_ctx.score,
                                is_test=file_ctx.is_test,
                            )
                        )
                else:
                    excluded.append(
                        ContextItemManifest(
                            path=file_ctx.relative_path,
                            tokens=estimate_tokens(file_ctx.content),
                            reasons=["Insufficient remaining token budget"],
                            score=file_ctx.score,
                            is_test=file_ctx.is_test,
                        )
                    )

        prompt_text = "\n\n".join(sections)
        # Strict hard enforcement: ensure total assembled prompt never exceeds max_tokens
        while prompt_text and estimate_tokens(prompt_text) > self.max_tokens:
            prompt_text = prompt_text[:-4].rstrip()

        used_tokens = estimate_tokens(prompt_text)

        manifest = ContextManifest(
            budget_tokens=self.max_tokens,
            used_tokens=used_tokens,
            included_items=included,
            excluded_items=excluded,
        )

        return BuiltContext(prompt_text=prompt_text, manifest=manifest)
