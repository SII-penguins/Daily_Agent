from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import yaml

from daily_agent.config import AppConfig
from daily_agent.feedback.profile import PreferenceProfile, _conflicts_with_positive_keyword, _specific_tag, build_feedback_profile
from daily_agent.storage import load_feedback_events, load_feedback_suggestions, load_material_library, set_feedback_suggestion_status, upsert_feedback_suggestions


@dataclass(frozen=True)
class SuggestionResponse:
    action: str
    suggestion_id: str
    reason: str | None = None


@dataclass
class SuggestionResponseResult:
    handled: bool = False
    applied: int = 0
    rejected: int = 0
    dry_run: bool = False
    responses: list[SuggestionResponse] = field(default_factory=list)
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def generate_feedback_suggestions(config: AppConfig, profile: PreferenceProfile | None = None) -> list[dict[str, Any]]:
    profile = profile or build_feedback_profile(config)
    existing = {(_suggestion_identity(item)) for item in load_feedback_suggestions(config)}
    now = _utc_now()
    suggestions = []
    for suggestion_type, value, reason in _candidate_suggestions(config, profile):
        identity = (suggestion_type, value)
        if identity in existing:
            continue
        suggestions.append(
            {
                "id": _suggestion_id(now, len(suggestions) + 1),
                "created_at": now,
                "updated_at": now,
                "type": suggestion_type,
                "value": value,
                "reason": reason,
                "source_event_ids": _source_event_ids(config, value),
                "status": "pending",
                "status_reason": None,
                "applied_at": None,
                "rejected_at": None,
            }
        )
    return suggestions


def save_feedback_suggestions(config: AppConfig, suggestions: list[dict[str, Any]]) -> None:
    upsert_feedback_suggestions(config, suggestions)


def apply_feedback_suggestion(config: AppConfig, suggestion_id: str) -> dict[str, Any]:
    suggestion = _find_suggestion(config, suggestion_id)
    if suggestion.get("status") != "pending":
        raise ValueError(f"Suggestion is not pending: {suggestion_id}")
    _apply_to_feedback_yaml(config, suggestion)
    set_feedback_suggestion_status(config, suggestion_id, "applied")
    return suggestion


def reject_feedback_suggestion(config: AppConfig, suggestion_id: str, reason: str | None = None) -> dict[str, Any]:
    suggestion = _find_suggestion(config, suggestion_id)
    set_feedback_suggestion_status(config, suggestion_id, "rejected", reason)
    return suggestion


def format_suggestion(suggestion: dict[str, Any]) -> str:
    return f"{suggestion.get('id')} [{suggestion.get('status')}] {suggestion.get('type')} = {suggestion.get('value')} — {suggestion.get('reason')}"


def parse_suggestion_response_text(text: str) -> list[SuggestionResponse]:
    responses = []
    for part in [item.strip() for item in re.split(r"[，,。；;\n]+", text or "") if item.strip()]:
        action = _parse_suggestion_action(part)
        suggestion_id = _parse_suggestion_id(part)
        if action and suggestion_id:
            responses.append(SuggestionResponse(action=action, suggestion_id=suggestion_id, reason=_parse_reject_reason(part) if action == "reject" else None))
    return responses


def process_suggestion_response_text(config: AppConfig, text: str, dry_run: bool = False) -> SuggestionResponseResult:
    responses = parse_suggestion_response_text(text)
    result = SuggestionResponseResult(handled=bool(responses), dry_run=dry_run, responses=responses)
    if dry_run or not responses:
        return result
    for response in responses:
        try:
            if response.action == "apply":
                result.suggestions.append(apply_feedback_suggestion(config, response.suggestion_id))
                result.applied += 1
            elif response.action == "reject":
                result.suggestions.append(reject_feedback_suggestion(config, response.suggestion_id, response.reason))
                result.rejected += 1
        except ValueError as exc:
            result.errors.append(str(exc))
    return result


def _parse_suggestion_action(text: str) -> str | None:
    if re.search(r"确认建议|接受建议|同意建议|应用建议|apply\s+suggestion", text, flags=re.IGNORECASE):
        return "apply"
    if re.search(r"拒绝建议|不要建议|忽略建议|reject\s+suggestion", text, flags=re.IGNORECASE):
        return "reject"
    return None


def _parse_suggestion_id(text: str) -> str | None:
    match = re.search(r"\bsug_[A-Za-z0-9_:-]+\b", text)
    return match.group(0) if match else None


def _parse_reject_reason(text: str) -> str | None:
    for pattern in [r"因为\s*(.+)$", r"reason\s*[:：]\s*(.+)$"]:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            reason = match.group(1).strip(" ：:，,。；; ")
            return reason or None
    suggestion_id = _parse_suggestion_id(text)
    if suggestion_id:
        tail = text.split(suggestion_id, 1)[1].strip(" ：:，,。；; ")
        return tail or None
    return None


def _candidate_suggestions(config: AppConfig, profile: PreferenceProfile) -> list[tuple[str, str, str]]:
    feedback = config.feedback or {}
    liked = feedback.get("liked", {}) or {}
    disliked = feedback.get("disliked", {}) or {}
    existing = {
        "liked.tag": {str(item) for item in liked.get("tags", [])},
        "liked.keyword": {str(item) for item in liked.get("keywords", [])},
        "disliked.tag": {str(item) for item in disliked.get("tags", [])},
        "disliked.keyword": {str(item) for item in disliked.get("keywords", [])},
    }
    candidates = []
    positive_keywords = {value for value, _ in profile.positive_keywords}
    for value, _ in profile.positive_tags[:3]:
        if value not in existing["liked.tag"] and _specific_tag(value):
            candidates.append(("liked.tag", value, f"来自 active feedback 的正向 tag 信号：{value}"))
    for value, _ in profile.negative_tags[:3]:
        if value not in existing["disliked.tag"] and _specific_tag(value) and not _conflicts_with_positive_keyword(value, positive_keywords):
            candidates.append(("disliked.tag", value, f"来自 active feedback 的负向 tag 信号：{value}"))
    for value, _ in profile.positive_keywords[:3]:
        if value not in existing["liked.keyword"]:
            candidates.append(("liked.keyword", value, f"来自 active feedback 的正向关键词信号：{value}"))
    for value, _ in profile.negative_keywords[:3]:
        if value not in existing["disliked.keyword"]:
            candidates.append(("disliked.keyword", value, f"来自 active feedback 的负向关键词信号：{value}"))
    return candidates


def _apply_to_feedback_yaml(config: AppConfig, suggestion: dict[str, Any]) -> None:
    payload = yaml.safe_load(config.feedback_path.read_text(encoding="utf-8")) or {}
    liked = payload.setdefault("liked", {})
    disliked = payload.setdefault("disliked", {})
    target_map = {
        "liked.tag": (liked, "tags"),
        "liked.keyword": (liked, "keywords"),
        "disliked.tag": (disliked, "tags"),
        "disliked.keyword": (disliked, "keywords"),
    }
    suggestion_type = suggestion.get("type")
    if suggestion_type not in target_map:
        raise ValueError(f"Unsupported suggestion type: {suggestion_type}")
    section, key = target_map[suggestion_type]
    values = section.setdefault(key, [])
    value = str(suggestion.get("value") or "")
    if value and value not in [str(item) for item in values]:
        values.append(value)
    config.feedback_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _find_suggestion(config: AppConfig, suggestion_id: str) -> dict[str, Any]:
    for suggestion in load_feedback_suggestions(config):
        if suggestion.get("id") == suggestion_id:
            return suggestion
    raise ValueError(f"Feedback suggestion not found: {suggestion_id}")


def _suggestion_identity(suggestion: dict[str, Any]) -> tuple[str, str]:
    return (str(suggestion.get("type") or ""), str(suggestion.get("value") or ""))


def _source_event_ids(config: AppConfig, value: str) -> list[str]:
    library = load_material_library(config)
    event_ids = []
    for event in load_feedback_events(config):
        if event.status != "active":
            continue
        material = library.get(event.key)
        tags = event.tags_snapshot or (material.tags if material else [])
        values = {str(item) for item in tags}
        values.update(str(item) for item in event.keywords)
        values.update(_note_values(event.note or ""))
        if value in values:
            event_ids.append(event.event_id)
    return event_ids[:5]


def _note_values(note: str) -> set[str]:
    values = set()
    for separator in ["，", "。", "；", ";", ","]:
        note = note.replace(separator, "|")
    for part in note.split("|"):
        text = part.strip()
        if text:
            values.add(text)
        for marker in ["主要还是关注", "主要关注", "更关注"]:
            if marker in text:
                value = text.split(marker, 1)[1].strip(" ：:，,。；; ")
                if value:
                    values.add(value)
    return values


def _suggestion_id(timestamp: str, index: int) -> str:
    compact = timestamp[:19].replace("-", "").replace(":", "").replace("T", "_")
    return f"sug_{compact}_{index:03d}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
