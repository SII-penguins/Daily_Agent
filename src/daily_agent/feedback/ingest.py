from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from daily_agent.config import AppConfig
from daily_agent.models import FeedbackEvent, utc_now_iso
from daily_agent.storage import resolve_published_item, upsert_feedback_event


@dataclass(frozen=True)
class ParsedFeedback:
    rank: int
    signal: str
    note: str


def parse_feedback_text(text: str) -> list[ParsedFeedback]:
    clauses = [part.strip() for part in re.split(r"[，,。；;\n]+", text or "") if part.strip()]
    if not clauses:
        raise ValueError("No feedback text found")
    parsed: list[ParsedFeedback] = []
    pending_ranks: list[int] = []
    pending_note: str | None = None
    last_index: int | None = None
    for clause in clauses:
        ranks = _parse_feedback_ranks(clause)
        signal = _parse_feedback_signal(clause)
        if ranks and signal:
            start = len(parsed)
            parsed.extend(ParsedFeedback(rank=rank, signal=signal, note=clause) for rank in ranks)
            last_index = start
            pending_ranks = []
            pending_note = None
        elif ranks:
            pending_ranks = ranks
            pending_note = clause
        elif signal and pending_ranks:
            note = f"{pending_note}，{clause}" if pending_note else clause
            start = len(parsed)
            parsed.extend(ParsedFeedback(rank=rank, signal=signal, note=note) for rank in pending_ranks)
            last_index = start
            pending_ranks = []
            pending_note = None
        elif pending_ranks:
            pending_note = f"{pending_note}，{clause}" if pending_note else clause
        elif last_index is not None:
            for index in range(last_index, len(parsed)):
                parsed[index] = ParsedFeedback(rank=parsed[index].rank, signal=parsed[index].signal, note=f"{parsed[index].note}，{clause}")
        else:
            raise ValueError(f"Cannot find item rank in: {clause}")
    if pending_ranks:
        raise ValueError(f"Cannot find like/dislike signal in: {pending_note}")
    return parsed


def record_feedback_text(
    config: AppConfig,
    text: str,
    date_selector: str | None = None,
    channel: str = "local",
    origin: str = "cli",
    keywords: list[str] | None = None,
    external_id_prefix: str | None = None,
    external_parent_id: str | None = None,
    source_updated_at: str | None = None,
    dry_run: bool = False,
) -> list[FeedbackEvent]:
    selector = date_selector or ("today" if re.search(r"今天|今日", text) else "latest")
    events = build_feedback_events(
        config=config,
        parsed_items=parse_feedback_text(text),
        date_selector=selector,
        channel=channel,
        origin=origin,
        keywords=keywords or [],
        external_id_prefix=external_id_prefix,
        external_parent_id=external_parent_id,
        source_updated_at=source_updated_at,
    )
    if not dry_run:
        for event in events:
            upsert_feedback_event(config, event)
    return events


def build_feedback_events(
    config: AppConfig,
    parsed_items: list[ParsedFeedback],
    date_selector: str,
    channel: str,
    origin: str,
    keywords: list[str],
    external_id_prefix: str | None = None,
    external_parent_id: str | None = None,
    source_updated_at: str | None = None,
) -> list[FeedbackEvent]:
    events: list[FeedbackEvent] = []
    for index, parsed in enumerate(parsed_items, start=1):
        run_date, item = resolve_published_item(config, date_selector, parsed.rank)
        events.append(
            build_feedback_event(
                item=item,
                run_date=run_date,
                rank=parsed.rank,
                signal=parsed.signal,
                channel=channel,
                keywords=keywords,
                note=parsed.note,
                origin=origin,
                external_id=f"{external_id_prefix}:{index}" if external_id_prefix else None,
                external_parent_id=external_parent_id,
                source_updated_at=source_updated_at,
            )
        )
    return events


def build_feedback_event(
    item: dict[str, Any],
    run_date: str,
    rank: int,
    signal: str,
    channel: str,
    keywords: list[str],
    note: str | None,
    origin: str = "cli",
    external_id: str | None = None,
    external_parent_id: str | None = None,
    source_updated_at: str | None = None,
) -> FeedbackEvent:
    timestamp = utc_now_iso()
    fingerprint = _feedback_fingerprint(origin, run_date, rank, signal, note or "")
    return FeedbackEvent(
        event_id=f"fb_{timestamp.replace('-', '').replace(':', '').replace('+', '').replace('T', '_')}_{rank}",
        created_at=timestamp,
        run_date=run_date,
        rank=rank,
        key=str(item["key"]),
        title=str(item["title"]),
        item_type=item["item_type"],
        signal=signal,
        channel=channel,
        tags_snapshot=[str(tag) for tag in item.get("tags", [])],
        keywords=[str(keyword) for keyword in keywords],
        note=note,
        origin=origin,
        external_id=external_id,
        external_parent_id=external_parent_id,
        source_updated_at=source_updated_at,
        fingerprint=fingerprint,
    )


def _parse_feedback_ranks(text: str) -> list[int]:
    values = []
    patterns = [r"第\s*(\d+)\s*条", r"#\s*(\d+)", r"(?<!\d)(\d+)\s*条"]
    for pattern in patterns:
        for value in re.findall(pattern, text):
            rank = int(value)
            if rank not in values:
                values.append(rank)
    return values


def _parse_feedback_signal(text: str) -> str | None:
    if re.search(r"不行|不好|没用|不喜欢|跳过|删除|一般|有出入|偏离|不相关|不太相关", text):
        return "dislike"
    if re.search(r"不错|喜欢|有用|推荐|值得|保留|(?<!不)好", text):
        return "like"
    return None


def _feedback_fingerprint(origin: str, run_date: str, rank: int, signal: str, note: str) -> str:
    raw = f"{origin}|{run_date}|{rank}|{signal}|{note}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
