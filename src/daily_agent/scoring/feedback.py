from __future__ import annotations

import math
from datetime import datetime, timezone

from daily_agent.config import AppConfig
from daily_agent.models import DigestItem, FeedbackEvent
from daily_agent.storage import load_feedback_events


def apply_feedback_scores(items: list[DigestItem], config: AppConfig) -> list[DigestItem]:
    feedback = config.feedback or {}
    liked = feedback.get("liked", {}) or {}
    disliked = feedback.get("disliked", {}) or {}
    liked_tags = _lower_set(liked.get("tags", []))
    liked_keywords = _lower_list(liked.get("keywords", []))
    disliked_tags = _lower_set(disliked.get("tags", []))
    disliked_keywords = _lower_list(disliked.get("keywords", []))
    event_config = feedback.get("events", {}) or {}
    events = [event for event in load_feedback_events(config) if event.status == "active"] if event_config.get("enabled", False) else []

    for item in items:
        tag_set = _lower_set(item.all_tags)
        text = _item_text(item)
        score = 0.0
        score += 5.0 * len(tag_set & liked_tags)
        score -= 6.0 * len(tag_set & disliked_tags)
        score += 4.0 * sum(1 for keyword in liked_keywords if keyword in text)
        score -= 5.0 * sum(1 for keyword in disliked_keywords if keyword in text)
        score += _event_feedback_score(item, tag_set, text, events, event_config)
        score = _clamp(score, float(event_config.get("min_feedback_score", -15)), float(event_config.get("max_feedback_score", 12)))
        if score:
            item.score += score
            item.score_breakdown["feedback"] = round(score, 3)
    return items


def _event_feedback_score(item: DigestItem, tag_set: set[str], text: str, events: list[FeedbackEvent], event_config: dict) -> float:
    score = 0.0
    now = datetime.now(timezone.utc)
    lookback_days = int(event_config.get("lookback_days", 90))
    half_life_days = max(float(event_config.get("half_life_days", 30)), 1.0)
    key = item.canonical_key()
    for event in events:
        created_at = _parse_datetime(event.created_at)
        if created_at:
            age_days = max((now - created_at).days, 0)
            if age_days > lookback_days:
                continue
            decay = 0.5 ** (age_days / half_life_days)
        else:
            decay = 1.0
        direction = 1.0 if event.signal == "like" else -1.0
        if event.key == key:
            exact_weight = float(event_config.get("exact_item_like_weight", 8) if direction > 0 else event_config.get("exact_item_dislike_weight", -10))
            score += exact_weight * decay
        event_tags = _lower_set(event.tags_snapshot)
        if tag_set & event_tags:
            tag_weight = float(event_config.get("tag_like_weight", 2) if direction > 0 else event_config.get("tag_dislike_weight", -3))
            score += tag_weight * len(tag_set & event_tags) * decay
        for keyword in _lower_list(event.keywords):
            if keyword in text:
                keyword_weight = float(event_config.get("keyword_like_weight", 3) if direction > 0 else event_config.get("keyword_dislike_weight", -4))
                score += keyword_weight * decay
    return score


def _item_text(item: DigestItem) -> str:
    parts = [
        item.title,
        item.abstract or "",
        item.repo_description or "",
        " ".join(item.categories),
        " ".join(item.source_tags),
    ]
    return " ".join(parts).lower()


def _lower_set(values: list[str]) -> set[str]:
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _lower_list(values: list[str]) -> list[str]:
    return [str(value).strip().lower() for value in values if str(value).strip()]


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


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(min(value, maximum), minimum)
