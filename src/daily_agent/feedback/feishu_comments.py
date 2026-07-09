from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from daily_agent.config import AppConfig
from daily_agent.delivery.feishu import BASE_URL, _headers, _load_cc_connect_feishu_config, _raise_for_feishu_error, _tenant_access_token
from daily_agent.feedback.ingest import record_feedback_text
from daily_agent.feedback.suggestions import process_suggestion_response_text
from daily_agent.models import FeedbackEvent
from daily_agent.secrets import credential_value
from daily_agent.storage import load_feishu_delivery_state


@dataclass
class FeishuFeedbackSyncResult:
    seen: int = 0
    parsed: int = 0
    recorded: int = 0
    unresolved: int = 0
    suggestions_applied: int = 0
    suggestions_rejected: int = 0
    errors: list[str] | None = None
    events: list[FeedbackEvent] | None = None
    suggestion_actions: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []
        if self.events is None:
            self.events = []
        if self.suggestion_actions is None:
            self.suggestion_actions = []


def sync_feishu_feedback(config: AppConfig, week: str = "latest", date_selector: str | None = None, dry_run: bool = False) -> FeishuFeedbackSyncResult:
    settings = config.feedback.get("feishu_comments", {}) or {}
    state = load_feishu_delivery_state(config)
    document_id = _resolve_document_id(state, week)
    if not document_id:
        raise ValueError(f"No Feishu document found for week {week}")

    creds = _load_cc_connect_feishu_config()
    app_id = _feedback_env_value(config, "app_id_env") or creds.get("app_id")
    app_secret = _feedback_env_value(config, "app_secret_env") or creds.get("app_secret")
    if not app_id or not app_secret:
        raise ValueError("Feishu credentials are missing")

    token = _tenant_access_token(app_id, app_secret, float(config.delivery.get("delivery", {}).get("feishu", {}).get("request_timeout_seconds", 30)))
    file_type = str(settings.get("file_type") or "docx")
    page_size = int(settings.get("page_size") or 50)
    include_replies = bool(settings.get("include_replies", True))

    result = FeishuFeedbackSyncResult()
    for comment in _list_comments(token, document_id, file_type, page_size):
        comment_id = str(comment.get("comment_id") or "")
        texts = []
        quote = str(comment.get("quote") or "").strip()
        if quote:
            texts.append(("feishu_comment", comment_id, None, quote, comment.get("update_time")))
        if include_replies:
            reply_list = comment.get("reply_list", {}) or {}
            for reply in reply_list.get("replies", []) or []:
                text = _reply_text(reply).strip()
                if text:
                    texts.append(("feishu_reply", str(reply.get("reply_id") or ""), comment_id, text, reply.get("update_time")))
            if comment.get("has_more") and comment_id:
                for reply in _list_replies(token, document_id, comment_id, file_type, page_size):
                    text = _reply_text(reply).strip()
                    if text:
                        texts.append(("feishu_reply", str(reply.get("reply_id") or ""), comment_id, text, reply.get("update_time")))
        for origin, external_id, parent_id, text, updated_at in _dedupe_texts(texts):
            result.seen += 1
            suggestion_result = process_suggestion_response_text(config, text, dry_run=dry_run)
            if suggestion_result.handled:
                result.suggestions_applied += suggestion_result.applied
                result.suggestions_rejected += suggestion_result.rejected
                for response in suggestion_result.responses:
                    result.suggestion_actions.append({"origin": origin, "external_id": external_id, "parent_id": parent_id, "action": response.action, "suggestion_id": response.suggestion_id, "reason": response.reason})
                if suggestion_result.errors:
                    result.errors.extend(suggestion_result.errors)
                    result.unresolved += len(suggestion_result.errors)
                continue
            try:
                events = record_feedback_text(
                    config,
                    text,
                    date_selector=date_selector,
                    channel="feishu",
                    origin=origin,
                    external_id_prefix=external_id or parent_id,
                    external_parent_id=parent_id,
                    source_updated_at=_timestamp_iso(updated_at),
                    dry_run=dry_run,
                )
            except ValueError:
                result.unresolved += 1
                continue
            result.parsed += len(events)
            result.events.extend(events)
            if not dry_run:
                result.recorded += len(events)
    return result


def _dedupe_texts(texts: list[tuple[str, str, str | None, str, Any]]) -> list[tuple[str, str, str | None, str, Any]]:
    by_identity: dict[tuple[str, str, str | None], tuple[str, str, str | None, str, Any]] = {}
    order: list[tuple[str, str, str | None]] = []
    for item in texts:
        origin, external_id, parent_id, _text, _updated_at = item
        identity = (origin, external_id, parent_id)
        if identity not in by_identity:
            order.append(identity)
        by_identity[identity] = item
    return [by_identity[identity] for identity in order]


def _resolve_document_id(state: dict[str, Any], week: str) -> str | None:
    weeks = state.get("weeks", {}) or {}
    key = sorted(weeks)[-1] if week == "latest" and weeks else week
    if not key:
        return None
    week_state = weeks.get(key) or {}
    return week_state.get("document_id")


def _list_comments(token: str, document_id: str, file_type: str, page_size: int) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    page_token = None
    while True:
        params: dict[str, Any] = {"file_type": file_type, "page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        response = httpx.get(f"{BASE_URL}/drive/v1/files/{document_id}/comments", headers=_headers(token), params=params, timeout=30)
        _raise_for_feishu_error(response)
        data = response.json().get("data", {}) or {}
        comments.extend(data.get("items", []) or [])
        if not data.get("has_more"):
            return comments
        page_token = data.get("page_token")
        if not page_token:
            return comments


def _list_replies(token: str, document_id: str, comment_id: str, file_type: str, page_size: int) -> list[dict[str, Any]]:
    replies: list[dict[str, Any]] = []
    page_token = None
    while True:
        params: dict[str, Any] = {"file_type": file_type, "page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        response = httpx.get(f"{BASE_URL}/drive/v1/files/{document_id}/comments/{comment_id}/replies", headers=_headers(token), params=params, timeout=30)
        _raise_for_feishu_error(response)
        data = response.json().get("data", {}) or {}
        replies.extend(data.get("items", []) or data.get("replies", []) or [])
        if not data.get("has_more"):
            return replies
        page_token = data.get("page_token")
        if not page_token:
            return replies


def _reply_text(reply: dict[str, Any]) -> str:
    parts = []
    content = reply.get("content", {}) or {}
    for element in content.get("elements", []) or []:
        text_run = element.get("text_run") or {}
        content_text = text_run.get("content") or text_run.get("text")
        if content_text:
            parts.append(str(content_text))
    return "".join(parts)


def _timestamp_iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return str(value)
    if timestamp > 10_000_000_000:
        timestamp = timestamp // 1000
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _feedback_env_value(config: AppConfig, key: str) -> str | None:
    feishu_config = config.delivery.get("delivery", {}).get("feishu", {}) or {}
    env_name = str(feishu_config.get(key) or "")
    return credential_value(env_name) if env_name else None
