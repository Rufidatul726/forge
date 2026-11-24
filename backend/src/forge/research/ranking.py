from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from forge.research.models import ResearchQuery, ResearchSource

KNOWN_OFFICIAL_DOC_PATTERNS = [
    re.compile(r"^docs?\.", re.IGNORECASE),
    re.compile(r"\.readthedocs\.io$", re.IGNORECASE),
    re.compile(r"\.github\.io$", re.IGNORECASE),
    re.compile(r"(python\.org|pydantic\.dev|tiangolo\.com|fastapi|pytest\.org|djangoproject\.com|react\.dev|vuejs\.org|angular\.dev)", re.IGNORECASE),
]

IGNORED_QUERY_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "fbclid",
    "gclid",
    "api_key",
    "apikey",
    "access_token",
    "token",
    "secret",
    "auth",
    "password",
    "key",
}


def normalize_url(url: str) -> str:
    """Normalize URL by lowercasing scheme and host, stripping tracking params and fragment."""
    parsed = urlsplit(url.strip())
    # Filter tracking params
    filtered_params = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in IGNORED_QUERY_PARAMS
    ]
    new_query = urlencode(filtered_params)
    path = parsed.path.rstrip("/") if parsed.path != "/" else "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, new_query, ""))


def classify_source_type(domain: str, url: str) -> str:
    """Determine source type deterministically based on domain and URL hierarchy."""
    domain_lower = domain.lower()
    if any(p.search(domain_lower) for p in KNOWN_OFFICIAL_DOC_PATTERNS):
        return "official_doc"
    if "github.com" in domain_lower or "gitlab.com" in domain_lower:
        return "repository"
    if any(k in domain_lower for k in ["stackoverflow.com", "reddit.com", "medium.com", "dev.to"]):
        return "community"
    if "/doc" in url.lower() or "/api" in url.lower() or "/guide" in url.lower() or "/reference" in url.lower():
        return "documentation"
    return "web"


def extract_keywords(text: str) -> list[str]:
    """Extract lowercased search terms ignoring common punctuation and short stop words."""
    tokens = re.findall(r"[a-zA-Z0-9_\-\.]+", text)
    stop_words = {
        "a", "an", "the", "in", "on", "at", "to", "for", "with", "from",
        "by", "and", "or", "is", "are", "was", "were", "be", "been", "how",
        "what", "why", "when", "which", "who", "does", "do", "did", "can",
    }
    keywords: list[str] = []
    for t in tokens:
        clean = t.strip("._-").lower()
        if len(clean) >= 2 and clean not in stop_words:
            keywords.append(clean)
    return sorted(set(keywords))


def compute_ranking_signals(source: ResearchSource, query: ResearchQuery) -> dict[str, float]:
    """Calculate deterministic, explainable scoring signals for a research source."""
    signals: dict[str, float] = {}
    keywords = extract_keywords(query.query)

    title_lower = source.page_title.lower()
    url_lower = source.canonical_url.lower()
    text_lower = source.extracted_text.lower()

    # 1. Title keyword matches
    title_hits = sum(1 for kw in keywords if kw in title_lower)
    if title_hits > 0:
        signals["title_relevance"] = round((title_hits / max(1, len(keywords))) * 30.0, 2)

    # 2. Heading keyword matches
    heading_hits = 0
    for h in source.extraction_warnings:  # headings stored or checked
        pass
    # Check headings directly from text lines starting with #
    heading_lines = [line.lower() for line in source.extracted_text.splitlines() if line.startswith("#")]
    heading_text = " ".join(heading_lines)
    for kw in keywords:
        if kw in heading_text:
            heading_hits += 1
    if heading_hits > 0:
        signals["heading_relevance"] = round((heading_hits / max(1, len(keywords))) * 25.0, 2)

    # 3. Content keyword coverage
    content_hits = sum(1 for kw in keywords if kw in text_lower)
    if content_hits > 0:
        signals["content_relevance"] = round((content_hits / max(1, len(keywords))) * 20.0, 2)

    # 4. Domain restriction match or preference
    if query.domain_restrictions:
        if any(d in source.domain.lower() for d in query.domain_restrictions):
            signals["domain_preference"] = 25.0
    elif any(p.search(source.domain) for p in KNOWN_OFFICIAL_DOC_PATTERNS):
        signals["official_doc_preference"] = 15.0

    # 5. Source type weighting
    if source.source_type == "official_doc":
        signals["source_type_weight"] = 15.0
    elif source.source_type == "documentation":
        signals["source_type_weight"] = 10.0
    elif source.source_type == "repository":
        signals["source_type_weight"] = 5.0

    # 6. Version matching
    if query.version:
        v_clean = query.version.lower().strip()
        if v_clean in title_lower or v_clean in url_lower or v_clean in heading_text:
            signals["version_match"] = 20.0
        elif re.search(r"v?\d+(\.\d+)+", url_lower):
            # Mismatch version in URL
            signals["version_mismatch_penalty"] = -10.0

    # 7. Search rank reciprocal signal
    if source.search_rank and source.search_rank > 0:
        signals["search_engine_rank"] = round(10.0 / source.search_rank, 2)

    return signals


def rank_and_deduplicate_sources(
    sources: list[ResearchSource],
    query: ResearchQuery,
) -> list[ResearchSource]:
    """Deduplicate sources by canonical URL and content hash, score them, and sort deterministically."""
    seen_urls: set[str] = set()
    seen_hashes: set[str] = set()
    deduped: list[ResearchSource] = []

    for src in sources:
        norm_canonical = normalize_url(src.canonical_url or src.original_url)
        norm_orig = normalize_url(src.original_url)

        if norm_canonical in seen_urls or norm_orig in seen_urls:
            continue

        if src.content_hash and src.content_hash in seen_hashes:
            continue

        seen_urls.add(norm_canonical)
        seen_urls.add(norm_orig)
        if src.content_hash:
            seen_hashes.add(src.content_hash)

        # Classify source type if not already set
        if not src.source_type or src.source_type == "web":
            src.source_type = classify_source_type(src.domain, src.canonical_url)

        # Compute explainable signals
        signals = compute_ranking_signals(src, query)
        total_score = round(sum(signals.values()), 2)
        src.ranking_signals = signals
        src.relevance_score = total_score

        deduped.append(src)

    # Deterministic sort: descending score, ascending search_rank, ascending canonical_url
    deduped.sort(
        key=lambda s: (
            -s.relevance_score,
            s.search_rank if s.search_rank is not None else 999,
            s.canonical_url,
        )
    )

    return deduped[:query.limit]
