from __future__ import annotations

import math
from datetime import datetime, timezone

from daily_agent.config import AppConfig, DomainConfig
from daily_agent.models import DigestItem, SelectedRecord
from daily_agent.scoring.publication import publication_scores
from daily_agent.scoring.relevance import min_topic_relevance_score, off_topic_score_penalty, topic_gate_enabled, topic_relevance_score


def score_items(
    items: list[DigestItem],
    config: AppConfig,
    history: dict[str, SelectedRecord] | None = None,
    target_date: datetime | None = None,
) -> list[DigestItem]:
    target = target_date or datetime.now(timezone.utc)
    history = history or {}
    for item in items:
        score = 0.0
        breakdown: dict[str, float] = {}
        matched_domain = _best_matching_domain(item, config.domains)
        if matched_domain:
            item.quota_group = item.quota_group or matched_domain.quota_group
            domain_score = matched_domain.priority * 20
            score += domain_score
            breakdown["domain"] = domain_score
        keyword_score = _keyword_score(item, config.domains)
        score += keyword_score
        breakdown["keywords"] = keyword_score
        relevance_score = topic_relevance_score(item, config)
        score += relevance_score
        breakdown["topic_relevance"] = relevance_score
        if topic_gate_enabled(config) and relevance_score < min_topic_relevance_score(config):
            penalty = off_topic_score_penalty(config)
            score += penalty
            breakdown["off_topic_penalty"] = penalty
        freshness_score = _freshness_score(item, target)
        score += freshness_score
        breakdown["freshness"] = freshness_score
        if item.source == "github":
            github_score = _github_quality_score(item)
            score += github_score
            breakdown["github_quality"] = github_score
        if item.item_type == "paper":
            impact_score = _scholarly_impact_score(item)
            score += impact_score
            breakdown["scholarly_impact"] = impact_score
            evidence_score = _evidence_score(item)
            score += evidence_score
            breakdown["evidence"] = evidence_score
            venue_score, publication_score = publication_scores(item, config.sources.get("publication_priority", {}))
            score += publication_score
            breakdown["publication"] = publication_score
            score += venue_score
            breakdown["venue"] = venue_score
            citation_discovery_score = _citation_discovery_score(item)
            score += citation_discovery_score
            if citation_discovery_score:
                breakdown["citation_discovery"] = citation_discovery_score
        if item.source == "arxiv" and item.pdf_url:
            score += 3
            breakdown["pdf"] = 3
        if item.is_historical_supplement:
            score -= 4
            breakdown["historical"] = -4
        history_score = _history_score(item, history)
        score += history_score
        breakdown["history"] = history_score
        feedback_score = item.score_breakdown.get("feedback", 0.0)
        score += feedback_score
        if feedback_score:
            breakdown["feedback"] = feedback_score
        item.score = round(score, 3)
        item.score_breakdown = breakdown
        item.fixed_tags = _infer_fixed_tags(item, config.fixed_tags)
        item.quota_group = item.quota_group or _infer_quota_group(item)
    return sorted(items, key=lambda item: item.score, reverse=True)


def select_items(items: list[DigestItem], config: AppConfig) -> list[DigestItem]:
    max_items = int(config.quota.get("max_items", 10))
    quantum_target = int(config.quota.get("quantum_target", 6))
    exploratory_target = int(config.quota.get("exploratory_target", 4))
    paper_target = int(config.quota.get("paper_target", 8))
    github_target = int(config.quota.get("github_target", 2))
    multiplier = max(1, int(config.quota.get("paper_review_multiplier", 2)))
    paper_review_target = max(paper_target, int(config.quota.get("paper_review_target", paper_target * multiplier)))
    candidate_limit = max(max_items, paper_review_target + github_target)

    selected: list[DigestItem] = []
    selected_keys: set[str] = set()

    def counts() -> dict[str, int]:
        return {
            "quantum": sum(1 for item in selected if item.quota_group == "quantum"),
            "exploratory": sum(1 for item in selected if item.quota_group != "quantum"),
            "paper": sum(1 for item in selected if item.item_type == "paper"),
            "repo": sum(1 for item in selected if item.item_type == "repo"),
        }

    def can_add(item: DigestItem, strict: bool) -> bool:
        current = counts()
        if strict:
            if item.item_type == "paper" and current["paper"] >= paper_target:
                return False
            if item.item_type == "repo" and current["repo"] >= github_target:
                return False
            if item.quota_group == "quantum" and current["quantum"] >= quantum_target:
                return False
            if item.quota_group != "quantum" and current["exploratory"] >= exploratory_target:
                return False
        return item.score > -50

    for strict in [True, False]:
        for item in items:
            if len(selected) >= candidate_limit:
                break
            key = item.canonical_key()
            if key in selected_keys or not can_add(item, strict):
                continue
            selected.append(item)
            selected_keys.add(key)
        if len(selected) >= candidate_limit:
            break

    return sorted(selected[:candidate_limit], key=lambda item: item.score, reverse=True)


def _best_matching_domain(item: DigestItem, domains: list[DomainConfig]) -> DomainConfig | None:
    text = _item_text(item)
    best: DomainConfig | None = None
    best_hits = 0
    for domain in domains:
        hits = sum(1 for keyword in domain.include_keywords if keyword.lower() in text)
        hits += sum(1 for category in domain.arxiv_categories if category.lower() in text)
        if hits > best_hits:
            best = domain
            best_hits = hits
    return best


def _keyword_score(item: DigestItem, domains: list[DomainConfig]) -> float:
    text = _item_text(item)
    score = 0.0
    for domain in domains:
        score += domain.priority * 2 * sum(1 for keyword in domain.include_keywords if keyword.lower() in text)
        score -= 4 * sum(1 for keyword in domain.exclude_keywords if keyword.lower() in text)
    return min(score, 35.0)


def _freshness_score(item: DigestItem, target: datetime) -> float:
    dt = _parse_datetime(item.updated_at) or _parse_datetime(item.published_at)
    if not dt:
        return 0.0
    age_days = max((target - dt).days, 0)
    return max(0.0, 18.0 * math.exp(-age_days / 21.0))


def _github_quality_score(item: DigestItem) -> float:
    stars = item.stars or 0
    forks = item.forks or 0
    score = min(math.log10(stars + 1) * 5, 18)
    score += min(math.log10(forks + 1) * 2, 6)
    if item.language:
        score += 2
    return score


def _scholarly_impact_score(item: DigestItem) -> float:
    citations = _number(item.raw.get("citation_count") or item.raw.get("cited_by_count"))
    influential = _number(item.raw.get("influential_citation_count"))
    return min(math.log10(citations + 1) * 4, 12) + min(math.log10(influential + 1) * 3, 6)


def _evidence_score(item: DigestItem) -> float:
    score = 0.0
    if item.abstract:
        score += 3.0
    if item.pdf_url:
        score += 2.0
    if item.doi:
        score += 2.0
    if item.raw.get("open_access_url"):
        score += 1.5
    return score


def _venue_score(item: DigestItem) -> float:
    return publication_scores(item)[0]


def _citation_discovery_score(item: DigestItem) -> float:
    return 6.0 if item.raw.get("citation_discovery") else 0.0


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _history_score(item: DigestItem, history: dict[str, SelectedRecord]) -> float:
    record = history.get(item.canonical_key())
    if not record:
        return 0.0
    if item.source == "arxiv" and item.arxiv_version and record.arxiv_version and item.arxiv_version != record.arxiv_version:
        item.update_label = "version_update"
        return 8.0
    if item.source == "github" and _is_major_github_update(item, record):
        item.update_label = "major_update"
        return 7.0
    return -100.0


def _is_major_github_update(item: DigestItem, record: SelectedRecord) -> bool:
    if _github_release_or_tag_changed(item, record):
        return True
    if item.raw.get("pushed_at") and record.github_pushed_at and item.raw["pushed_at"] != record.github_pushed_at:
        stars_delta = (item.stars or 0) - (record.stars or 0)
        threshold = 50 if (record.stars or 0) < 1000 else 300
        return stars_delta >= threshold
    return False


def _github_release_or_tag_changed(item: DigestItem, record: SelectedRecord) -> bool:
    release_tag = item.raw.get("latest_release_tag")
    if release_tag and record.github_latest_release_tag and release_tag != record.github_latest_release_tag:
        return True
    release_published_at = item.raw.get("latest_release_published_at")
    if release_published_at and record.github_latest_release_published_at and release_published_at != record.github_latest_release_published_at:
        return True
    tag_name = item.raw.get("latest_tag_name")
    if tag_name and record.github_latest_tag_name and tag_name != record.github_latest_tag_name:
        return True
    return False


def _infer_fixed_tags(item: DigestItem, fixed_tags: list[str]) -> list[str]:
    text = _item_text(item).replace("-", "_").replace(" ", "_")
    tags = []
    for tag in fixed_tags:
        normalized = tag.lower().replace("-", "_")
        if normalized in text:
            tags.append(tag)
    if not tags:
        if item.source == "arxiv" and any("quant" in category.lower() for category in item.categories):
            tags.append("quantum_ai")
        elif item.source == "github" and "agent" in _item_text(item):
            tags.append("agent")
    return tags


def _infer_quota_group(item: DigestItem) -> str:
    text = _item_text(item)
    if "quant" in text or any("quant" in category.lower() for category in item.categories):
        return "quantum"
    return "exploratory"


def _item_text(item: DigestItem) -> str:
    return " ".join(
        [
            item.title,
            item.abstract or "",
            item.repo_description or "",
            " ".join(item.categories),
            item.language or "",
        ]
    ).lower()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None
