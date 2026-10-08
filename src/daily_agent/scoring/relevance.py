from __future__ import annotations

import re
from typing import Any

from daily_agent.config import AppConfig, DomainConfig
from daily_agent.models import DigestItem, MaterialRecord

LOW_SIGNAL_KEYWORDS = {
    "su(2)",
    "quantum",
    "quantum mechanics",
    "physics",
    "mathematics",
    "algorithm",
    "observable",
}

SOURCE_NAMES = {
    "arxiv",
    "github",
    "openalex",
    "semantic_scholar",
    "google_scholar",
    "crossref",
    "core",
    "dblp",
    "ieee",
    "openreview",
    "pmlr",
    "neurips",
    "nature",
}


def topic_relevance_score(item: DigestItem | MaterialRecord, config: AppConfig) -> float:
    """Score whether an item is actually about a configured interest.

    Source provenance tags are intentionally excluded. A paper fetched by the
    "quantum compilation" query still needs title/abstract/category evidence.
    """

    title = _normalize(_field(item, "title"))
    body = _normalize(" ".join(_body_parts(item)))
    categories = _normalize(" ".join(_list_field(item, "categories")))
    best = 0.0
    for domain in config.domains:
        best = max(best, _domain_topic_score(domain, title, body, categories, item))
    return round(min(best, 50.0), 3)


def topic_gate_enabled(config: AppConfig) -> bool:
    selection = config.sources.get("selection", {}) or {}
    return bool(selection.get("topic_relevance_gate_enabled", True))


def min_topic_relevance_score(config: AppConfig) -> float:
    selection = config.sources.get("selection", {}) or {}
    return float(selection.get("min_topic_relevance_score", 6.0))


def off_topic_score_penalty(config: AppConfig) -> float:
    selection = config.sources.get("selection", {}) or {}
    return float(selection.get("off_topic_score_penalty", -120.0))


def passes_topic_gate(item: DigestItem | MaterialRecord, config: AppConfig) -> bool:
    if not topic_gate_enabled(config):
        return True
    return topic_relevance_score(item, config) >= min_topic_relevance_score(config)


def annotate_topic_relevance(item: DigestItem | MaterialRecord, config: AppConfig) -> float:
    relevance = topic_relevance_score(item, config)
    breakdown = dict(getattr(item, "score_breakdown", {}) or {})
    breakdown["topic_relevance"] = relevance
    breakdown.pop("off_topic_penalty", None)
    if topic_gate_enabled(config) and relevance < min_topic_relevance_score(config):
        breakdown["off_topic_penalty"] = off_topic_score_penalty(config)
    item.score_breakdown = breakdown
    return relevance


def _domain_topic_score(domain: DomainConfig, title: str, body: str, categories: str, item: DigestItem | MaterialRecord) -> float:
    score = 0.0
    strong_hits = 0
    for keyword in _domain_keywords(domain):
        normalized = _normalize(keyword)
        if not normalized or normalized in SOURCE_NAMES:
            continue
        weak = _is_low_signal_keyword(normalized)
        if _phrase_in(normalized, title):
            score += 0.6 if weak else 6.0
            strong_hits += 0 if weak else 1
        elif _phrase_in(normalized, body):
            score += 0.4 if weak else 4.0
            strong_hits += 0 if weak else 1
        elif _phrase_in(normalized, categories):
            score += 0.3 if weak else 2.0
            strong_hits += 0 if weak else 1

    implicit_score, implicit_strong_hits = _implicit_domain_score(domain, title, body, categories, item)
    score += implicit_score
    strong_hits += implicit_strong_hits

    arxiv_categories = {category.lower() for category in domain.arxiv_categories}
    item_categories = {category.lower() for category in _list_field(item, "categories")}
    if _field(item, "source") == "arxiv" and item_categories & arxiv_categories:
        score += 3.0 if strong_hits else 1.0
    if _venue_bonus(item, domain, title, body):
        score += 3.0
    return score


def _implicit_domain_score(
    domain: DomainConfig,
    title: str,
    body: str,
    categories: str,
    item: DigestItem | MaterialRecord,
) -> tuple[float, int]:
    text = " ".join([title, body, categories])
    source = _field(item, "source")
    score = 0.0
    strong_hits = 0
    if domain.name == "embodied_and_agents":
        for keyword, title_weight, body_weight in [
            ("agent", 6.0, 4.0),
            ("agents", 6.0, 4.0),
            ("mcp", 5.0, 4.0),
            ("rag", 5.0, 4.0),
            ("tool calling", 5.0, 4.0),
            ("world model", 6.0, 4.0),
            ("vla", 6.0, 4.0),
        ]:
            if _phrase_in(keyword, title):
                score += title_weight
                strong_hits += 1
            elif _phrase_in(keyword, body):
                score += body_weight
                strong_hits += 1
    if domain.quota_group == "quantum":
        for keyword in ["qiskit", "qubit", "quantum gate", "quantum computer", "clifford", "nisq"]:
            if _phrase_in(keyword, text):
                score += 2.0
                strong_hits += 1
    if source == "github" and _phrase_in("agent", text):
        score += 2.0
        strong_hits += 1
    return score, strong_hits


def _domain_keywords(domain: DomainConfig) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for keyword in [*domain.base_include_keywords, *domain.expanded_keywords, *domain.include_keywords]:
        key = _normalize(keyword)
        if key and key not in seen:
            seen.add(key)
            values.append(keyword)
    return values


def _is_low_signal_keyword(keyword: str) -> bool:
    if keyword in LOW_SIGNAL_KEYWORDS:
        return True
    if len(keyword) <= 2 and keyword not in {"qec", "vqe"}:
        return True
    if keyword in {"circuit", "compiler", "routing", "logic"}:
        return True
    return False


def _phrase_in(needle: str, haystack: str) -> bool:
    if not needle or not haystack:
        return False
    if re.fullmatch(r"[a-z0-9]+", needle):
        return bool(re.search(rf"\b{re.escape(needle)}\b", haystack))
    return needle in haystack


def _venue_bonus(item: DigestItem | MaterialRecord, domain: DomainConfig, title: str, body: str) -> bool:
    venue = _normalize(str(_raw(item).get("venue") or ""))
    source = _normalize(_field(item, "source"))
    if not any(name in " ".join([venue, source]) for name in ["iclr", "icml", "neurips", "nips", "openreview", "pmlr"]):
        return False
    return _domain_topic_score_without_venue(domain, title, body) >= 3.0


def _domain_topic_score_without_venue(domain: DomainConfig, title: str, body: str) -> float:
    for keyword in _domain_keywords(domain):
        normalized = _normalize(keyword)
        if not _is_low_signal_keyword(normalized) and (_phrase_in(normalized, title) or _phrase_in(normalized, body)):
            return 3.0
    return 0.0


def _body_parts(item: DigestItem | MaterialRecord) -> list[str]:
    parts = [
        _field(item, "abstract"),
        _field(item, "repo_description"),
        _field(item, "language"),
        str(_raw(item).get("venue") or ""),
    ]
    if isinstance(item, MaterialRecord):
        sources = (item.evidence or {}).get("sources", {}) or {}
        for source in sources.values():
            if isinstance(source, dict):
                parts.extend([str(source.get("title") or ""), str(source.get("abstract") or "")])
    return [part for part in parts if part]


def _field(item: DigestItem | MaterialRecord, name: str) -> str:
    value = getattr(item, name, "")
    return str(value or "")


def _list_field(item: DigestItem | MaterialRecord, name: str) -> list[str]:
    value = getattr(item, name, []) or []
    return [str(entry) for entry in value if entry]


def _raw(item: DigestItem | MaterialRecord) -> dict[str, Any]:
    value = getattr(item, "raw", {}) or {}
    return value if isinstance(value, dict) else {}


def _normalize(text: str) -> str:
    normalized = text.lower()
    normalized = normalized.replace("_", " ").replace("-", " ")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()
