from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class ResearchStatus(str, Enum):
    PENDING = "pending"
    RETRIEVING = "retrieving"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


class FindingKind(str, Enum):
    VERIFIED_FACT = "verified_fact"
    INFERENCE = "inference"
    RECOMMENDATION = "recommendation"
    UNVERIFIED_ASSUMPTION = "unverified_assumption"
    UNRESOLVED_QUESTION = "unresolved_question"


class ResearchQuery(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=20)
    domain_restrictions: list[str] = Field(default_factory=list)
    recency: str | None = None
    package: str | None = None
    version: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("Query cannot be blank")
        return clean

    @field_validator("domain_restrictions", mode="before")
    @classmethod
    def clean_domains(cls, value: object) -> list[str]:
        if isinstance(value, str):
            domains = [d.strip().lower() for d in value.split(",") if d.strip()]
            return domains
        if isinstance(value, list):
            return [str(d).strip().lower() for d in value if str(d).strip()]
        return []


class SearchResult(BaseModel):
    url: str
    title: str
    snippet: str
    rank: int
    domain: str
    source_type: str = "web"
    score: float = 0.0


class RetrievedDocument(BaseModel):
    canonical_url: str
    original_url: str
    title: str
    domain: str
    content_type: str
    raw_content: str | None = None
    extracted_text: str = ""
    headings: list[str] = Field(default_factory=list)
    code_blocks: list[str] = Field(default_factory=list)
    content_hash: str = ""
    retrieval_timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    status: str = "success"  # "success", "failed", "cached"
    error_message: str | None = None
    publication_date: str | None = None
    modification_date: str | None = None
    extraction_warnings: list[str] = Field(default_factory=list)


class ResearchSource(BaseModel):
    source_id: str
    canonical_url: str
    original_url: str
    page_title: str
    domain: str
    publication_or_modification_date: str | None = None
    retrieval_timestamp: str
    content_type: str
    extracted_text: str
    content_hash: str
    retrieval_status: str = "success"
    search_rank: int | None = None
    relevance_score: float = 0.0
    source_type: str = "documentation"
    extraction_warnings: list[str] = Field(default_factory=list)
    ranking_signals: dict[str, float] = Field(default_factory=dict)


class EvidenceExcerpt(BaseModel):
    excerpt_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    source_id: str
    exact_text: str
    section_heading: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    relevance_score: float = 0.0
    relevance_reasons: list[str] = Field(default_factory=list)
    supporting_claim: str | None = None


class Citation(BaseModel):
    citation_id: str = Field(default_factory=lambda: uuid4().hex[:8])
    source_id: str
    source_title: str
    canonical_url: str
    domain: str
    retrieval_date: str
    relevant_excerpt: str
    section_heading: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    is_verified: bool = False


class ResearchFinding(BaseModel):
    claim: str
    kind: FindingKind
    supporting_citation_ids: list[str] = Field(default_factory=list)
    confidence_score: float = 1.0


class ResearchReport(BaseModel):
    research_id: str = Field(default_factory=lambda: uuid4().hex[:16])
    query: ResearchQuery
    sources: list[ResearchSource] = Field(default_factory=list)
    evidence_excerpts: list[EvidenceExcerpt] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    verified_facts: list[str] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    unverified_assumptions: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    conflicting_sources: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    status: ResearchStatus = ResearchStatus.COMPLETED
    warnings: list[str] = Field(default_factory=list)
