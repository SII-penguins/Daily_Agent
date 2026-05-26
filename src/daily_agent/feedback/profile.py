from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from daily_agent.config import AppConfig
from daily_agent.models import FeedbackEvent
from daily_agent.storage import load_feedback_events, load_material_library


@dataclass
class ItemFeedbackSummary:
    key: str
    title: str
    active_likes: int = 0
    active_dislikes: int = 0
    ignored: int = 0
    feedback_snapshot: float | None = None
    event_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class PreferenceProfile:
    active_events: int
    ignored_events: int
    positive_tags: list[tuple[str, float]]
    negative_tags: list[tuple[str, float]]
    positive_keywords: list[tuple[str, float]]
    negative_keywords: list[tuple[str, float]]
    item_summaries: list[ItemFeedbackSummary]
    recommendations: list[str]


def build_feedback_profile(config: AppConfig, limit: int = 20, include_ignored: bool = False) -> PreferenceProfile:
    events = load_feedback_events(config)
    library = load_material_library(config)
    positive_tags: Counter[str] = Counter()
    negative_tags: Counter[str] = Counter()
    positive_keywords: Counter[str] = Counter()
    negative_keywords: Counter[str] = Counter()
    grouped: dict[str, ItemFeedbackSummary] = {}
    active_count = 0
    ignored_count = 0

    for event in events:
        material = library.get(event.key)
        summary = grouped.setdefault(
            event.key,
            ItemFeedbackSummary(
                key=event.key,
                title=event.title,
                feedback_snapshot=(material.score_breakdown.get("feedback") if material else None),
            ),
        )
        if event.status != "active":
            ignored_count += 1
            summary.ignored += 1
            if not include_ignored:
                continue
        else:
            active_count += 1
            summary.event_ids.append(event.event_id)
            if event.signal == "like":
                summary.active_likes += 1
            else:
                summary.active_dislikes += 1
            tags = event.tags_snapshot or (material.tags if material else [])
            target_tags = positive_tags if event.signal == "like" else negative_tags
            for tag in tags:
                target_tags[str(tag)] += 1
            positive_hints, negative_hints = _note_hints(event)
            for keyword in event.keywords:
                (positive_keywords if event.signal == "like" else negative_keywords)[str(keyword)] += 1
            for keyword in positive_hints:
                positive_keywords[keyword] += 1
            for keyword in negative_hints:
                negative_keywords[keyword] += 1
        if event.note:
            summary.notes.append(event.note)

    summaries = sorted(grouped.values(), key=lambda item: item.active_likes + item.active_dislikes + item.ignored, reverse=True)[:limit]
    profile = PreferenceProfile(
        active_events=active_count,
        ignored_events=ignored_count,
        positive_tags=_top(positive_tags),
        negative_tags=_top(negative_tags),
        positive_keywords=_top(positive_keywords),
        negative_keywords=_top(negative_keywords),
        item_summaries=summaries,
        recommendations=[],
    )
    profile.recommendations = _recommendations(config, profile)
    return profile


def render_feedback_profile(profile: PreferenceProfile, config: AppConfig) -> str:
    feedback = config.feedback or {}
    liked = feedback.get("liked", {}) or {}
    disliked = feedback.get("disliked", {}) or {}
    lines = [
        "Feedback profile (read-only)",
        f"Active events: {profile.active_events}, ignored: {profile.ignored_events}",
        "",
        "Positive signals:",
    ]
    lines.extend(_render_pairs(profile.positive_tags, "tag"))
    lines.extend(_render_pairs(profile.positive_keywords, "keyword"))
    if len(lines) == 4:
        lines.append("- Not enough active positive feedback yet")
    lines.append("")
    lines.append("Negative signals:")
    before_negative = len(lines)
    lines.extend(_render_pairs(profile.negative_tags, "tag"))
    lines.extend(_render_pairs(profile.negative_keywords, "keyword"))
    if len(lines) == before_negative:
        lines.append("- Not enough active negative feedback yet")
    lines.extend(
        [
            "",
            "Current manual config:",
            f"- liked.tags: {liked.get('tags', [])}",
            f"- liked.keywords: {liked.get('keywords', [])}",
            f"- disliked.tags: {disliked.get('tags', [])}",
            f"- disliked.keywords: {disliked.get('keywords', [])}",
            "",
            "Recent item summaries:",
        ]
    )
    if profile.item_summaries:
        for item in profile.item_summaries:
            lines.append(f"- {item.title}: active like={item.active_likes}, dislike={item.active_dislikes}, ignored={item.ignored}, feedback_snapshot={item.feedback_snapshot}")
            for note in item.notes[:2]:
                lines.append(f"  note: {note}")
    else:
        lines.append("- No feedback events yet")
    lines.extend(["", "Recommendations (not applied):"])
    if profile.recommendations:
        lines.extend(f"- {item}" for item in profile.recommendations)
    else:
        lines.append("- Not enough active feedback to recommend config changes yet")
    return "\n".join(lines)


def _note_hints(event: FeedbackEvent) -> tuple[list[str], list[str]]:
    note = event.note or ""
    positive = []
    negative = []
    for pattern in [r"(?:主要还是关注|主要关注|更关注)([^，。；;]+)"]:
        for match in re.findall(pattern, note, flags=re.IGNORECASE):
            keyword = _clean_hint(match)
            if keyword:
                positive.append(keyword)
    for pattern in [r"([^，。；;]+?)(?:有出入|不相关|不太相关|偏离)"]:
        for match in re.findall(pattern, note, flags=re.IGNORECASE):
            keyword = _clean_hint(match)
            if keyword:
                negative.append(keyword)
    return positive, negative


def _clean_hint(value: str) -> str:
    value = re.sub(r"^(和|跟|与|我|我的|关注的|领域|方向|是)+", "", value.strip(), flags=re.IGNORECASE)
    value = re.sub(r"(领域|方向)$", "", value.strip(), flags=re.IGNORECASE)
    return value.strip(" ：:，,。；; ")


def _top(counter: Counter[str], limit: int = 8) -> list[tuple[str, float]]:
    return [(key, float(value)) for key, value in counter.most_common(limit) if key]


def _render_pairs(values: list[tuple[str, float]], kind: str) -> list[str]:
    return [f"- {kind}: {key} ({score:g})" for key, score in values]


def _specific_tag(tag: str) -> bool:
    return tag not in {"quantum_ai", "embodied_and_agents", "agent", "search", "rag"}


def _conflicts_with_positive_keyword(tag: str, positive_keywords: set[str]) -> bool:
    normalized = tag.replace("_", "").replace("-", "").lower()
    for keyword in positive_keywords:
        compact = keyword.replace(" ", "").replace("_", "").replace("-", "").lower()
        if "量子线路" in keyword and normalized in {"quantumcircuit", "quantumcircuits"}:
            return True
        if compact and (compact in normalized or normalized in compact):
            return True
    return False


def _recommendations(config: AppConfig, profile: PreferenceProfile) -> list[str]:
    feedback = config.feedback or {}
    liked = feedback.get("liked", {}) or {}
    disliked = feedback.get("disliked", {}) or {}
    existing_liked_tags = {str(item) for item in liked.get("tags", [])}
    existing_disliked_tags = {str(item) for item in disliked.get("tags", [])}
    existing_liked_keywords = {str(item) for item in liked.get("keywords", [])}
    existing_disliked_keywords = {str(item) for item in disliked.get("keywords", [])}
    recommendations = []
    for tag, _ in profile.positive_tags[:3]:
        if tag not in existing_liked_tags and _specific_tag(tag):
            recommendations.append(f"Consider adding liked tag: {tag}")
    positive_keywords = {keyword for keyword, _ in profile.positive_keywords}
    for tag, _ in profile.negative_tags[:3]:
        if tag not in existing_disliked_tags and _specific_tag(tag) and not _conflicts_with_positive_keyword(tag, positive_keywords):
            recommendations.append(f"Consider adding disliked tag: {tag}")
    for keyword, _ in profile.positive_keywords[:3]:
        if keyword not in existing_liked_keywords:
            recommendations.append(f"Consider adding liked keyword: {keyword}")
    for keyword, _ in profile.negative_keywords[:3]:
        if keyword not in existing_disliked_keywords:
            recommendations.append(f"Consider adding disliked keyword: {keyword}")
    if profile.active_events < 2 and not recommendations:
        recommendations.append("Collect more active feedback before changing long-term rules")
    return recommendations[:10]
