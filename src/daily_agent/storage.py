from __future__ import annotations

import json
import re
import csv
import xml.etree.ElementTree as ET
from io import StringIO
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
import shutil
from typing import Any, Iterable

from daily_agent.config import AppConfig
from daily_agent.models import ApprovedItem, DigestItem, EditorialDraft, EditorialReview, FeedbackEvent, MaterialRecord, RunStatus, SelectedRecord

DATE_MARKER_PREFIX = "<!-- daily-agent-date:"
HISTORY_INDEX_NAME = "history_index.json"
MATERIAL_LIBRARY_NAME = "library.json"
PUBLISHED_INDEX_NAME = "published_index.json"
FEEDBACK_EVENTS_NAME = "feedback_events.json"
FEISHU_DELIVERY_NAME = "feishu_delivery.json"
FEEDBACK_SUGGESTIONS_NAME = "feedback_suggestions.json"
HEALTH_STATE_NAME = "health.json"


def ensure_storage_dirs(config: AppConfig) -> None:
    for path in [config.reports_dir, config.selected_dir, config.state_dir, config.logs_dir, materials_dir(config)]:
        path.mkdir(parents=True, exist_ok=True)


def materials_dir(config: AppConfig) -> Path:
    return config.root / "data" / "materials"


def editorial_dir(config: AppConfig, run_date: date) -> Path:
    return config.root / "data" / "editorial" / run_date.isoformat()


def load_material_library(config: AppConfig) -> dict[str, MaterialRecord]:
    ensure_storage_dirs(config)
    path = materials_dir(config) / MATERIAL_LIBRARY_NAME
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    library: dict[str, MaterialRecord] = {}
    for key, raw in payload.get("items", {}).items():
        try:
            record = MaterialRecord.from_dict(raw)
        except TypeError:
            continue
        library[key] = record
    return library


def write_material_library(config: AppConfig, library: dict[str, MaterialRecord]) -> Path:
    ensure_storage_dirs(config)
    path = materials_dir(config) / MATERIAL_LIBRARY_NAME
    payload = {
        "schema_version": 1,
        "updated_at": _utc_now(),
        "items": {key: record.to_dict() for key, record in sorted(library.items())},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def upsert_materials(config: AppConfig, items: list[DigestItem], run_date: date) -> dict[str, MaterialRecord]:
    library = load_material_library(config)
    seen_at = datetime(run_date.year, run_date.month, run_date.day, tzinfo=timezone.utc).isoformat()
    for item in items:
        key = item.canonical_key()
        incoming = MaterialRecord.from_item(item, seen_at=seen_at)
        existing = library.get(key)
        if existing:
            incoming.first_seen_at = existing.first_seen_at
            incoming.published_dates = existing.published_dates
            incoming.quality_status = existing.quality_status if existing.quality_status in {"published", "archived", "rejected"} else incoming.quality_status
            incoming.readme_excerpt = existing.readme_excerpt or incoming.readme_excerpt
            incoming.paper_text_excerpt = existing.paper_text_excerpt or incoming.paper_text_excerpt
            incoming.paper_text_status = incoming.paper_text_status or existing.paper_text_status
            incoming.detail = {**existing.detail, **incoming.detail}
            incoming.source_aliases = {**existing.source_aliases, **incoming.source_aliases}
            incoming.evidence = _merge_evidence(existing.evidence, incoming.evidence)
        library[key] = incoming
    write_material_library(config, library)
    return library


def select_library_candidates(config: AppConfig, library: dict[str, MaterialRecord], run_date: date) -> list[MaterialRecord]:
    repeat_days = int(config.sources.get("selection", {}).get("repeat_suppression_days", 30))
    candidates = []
    for record in library.values():
        if record.quality_status in {"rejected", "archived"}:
            continue
        if _is_recent_repeat_without_update(record, run_date, repeat_days):
            continue
        candidates.append(record)
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates


def mark_materials_published(config: AppConfig, records: list[MaterialRecord], run_date: date) -> None:
    library = load_material_library(config)
    stamp = run_date.isoformat()
    for record in records:
        stored = library.get(record.key, record)
        stored.readme_excerpt = record.readme_excerpt or stored.readme_excerpt
        stored.paper_text_excerpt = record.paper_text_excerpt or stored.paper_text_excerpt
        stored.paper_text_status = record.paper_text_status or stored.paper_text_status
        stored.raw = {**stored.raw, **record.raw}
        stored.evidence = _merge_evidence(stored.evidence, record.evidence)
        if stamp not in stored.published_dates:
            stored.published_dates.append(stamp)
        stored.quality_status = "published"
        stored.update_label = None
        library[stored.key] = stored
    write_material_library(config, library)


def write_editorial_artifacts(
    config: AppConfig,
    run_date: date,
    shortlist: list[MaterialRecord],
    drafts: list[EditorialDraft],
    reviews: list[EditorialReview],
    approvals: list[ApprovedItem],
) -> Path:
    path = editorial_dir(config, run_date)
    path.mkdir(parents=True, exist_ok=True)
    _write_json(path / "shortlist.json", [record.to_dict() for record in shortlist])
    _write_json(path / "writer_draft.json", [draft.to_dict() for draft in drafts])
    _write_json(path / "editor_review.json", [review.to_dict() for review in reviews])
    _write_json(path / "approval.json", [approval.to_dict() for approval in approvals])
    return path


def load_history(config: AppConfig, today: date | None = None) -> dict[str, SelectedRecord]:
    ensure_storage_dirs(config)
    today = today or date.today()
    cutoff = today - timedelta(days=int(config.sources.get("selection", {}).get("repeat_suppression_days", 30)))
    records = _load_history_index(config, cutoff)
    for record in _iter_selected_records(config.selected_dir.glob("selected-*.json")):
        selected_at = _parse_date(record.selected_at)
        if selected_at and selected_at >= cutoff:
            records[record.key] = record
    _write_history_index(config, records.values(), today)
    return records


def write_selected(config: AppConfig, items: list[DigestItem | MaterialRecord | ApprovedItem], run_date: date, dry_run: bool = False) -> Path:
    ensure_storage_dirs(config)
    name = f"selected-{run_date.isoformat()}.dry-run.json" if dry_run else f"selected-{run_date.isoformat()}.json"
    path = config.selected_dir / name
    records = [_selected_record_from_any(item, rank=index) for index, item in enumerate(items, start=1)]
    payload = {
        "date": run_date.isoformat(),
        "dry_run": dry_run,
        "selected": [record.to_dict() for record in records],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if not dry_run:
        history = load_history(config, run_date)
        for record in records:
            history[record.key] = record
        _write_history_index(config, history.values(), run_date)
    return path


def write_bibtex_export(config: AppConfig, items: list[DigestItem | MaterialRecord | ApprovedItem], run_date: date, dry_run: bool = False) -> Path:
    ensure_storage_dirs(config)
    suffix = ".dry-run" if dry_run else ""
    path = config.selected_dir / f"selected-{run_date.isoformat()}{suffix}.bib"
    materials = [_material_from_any(item) for item in items]
    papers = [material for material in materials if material.item_type == "paper"]
    used_keys: set[str] = set()
    entries = [_bibtex_entry(material, used_keys) for material in papers]
    path.write_text("\n\n".join(entries).rstrip() + ("\n" if entries else ""), encoding="utf-8")
    return path


def write_ris_export(config: AppConfig, items: list[DigestItem | MaterialRecord | ApprovedItem], run_date: date, dry_run: bool = False) -> Path:
    ensure_storage_dirs(config)
    suffix = ".dry-run" if dry_run else ""
    path = config.selected_dir / f"selected-{run_date.isoformat()}{suffix}.ris"
    papers = [material for material in [_material_from_any(item) for item in items] if material.item_type == "paper"]
    entries = [_ris_entry(material) for material in papers]
    path.write_text("\n\n".join(entries).rstrip() + ("\n" if entries else ""), encoding="utf-8")
    return path


def write_endnote_xml_export(config: AppConfig, items: list[DigestItem | MaterialRecord | ApprovedItem], run_date: date, dry_run: bool = False) -> Path:
    ensure_storage_dirs(config)
    suffix = ".dry-run" if dry_run else ""
    path = config.selected_dir / f"selected-{run_date.isoformat()}{suffix}.xml"
    papers = [material for material in [_material_from_any(item) for item in items] if material.item_type == "paper"]
    root = ET.Element("xml")
    records = ET.SubElement(root, "records")
    for material in papers:
        records.append(_endnote_record(material))
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return path


def write_csv_export(config: AppConfig, items: list[DigestItem | MaterialRecord | ApprovedItem], run_date: date, dry_run: bool = False) -> Path:
    ensure_storage_dirs(config)
    suffix = ".dry-run" if dry_run else ""
    path = config.selected_dir / f"selected-{run_date.isoformat()}{suffix}.csv"
    materials = [_material_from_any(item) for item in items]
    output = StringIO()
    fieldnames = ["rank", "item_type", "source", "title", "authors", "doi", "url", "pdf_url", "score", "tags"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for rank, material in enumerate(materials, start=1):
        writer.writerow(_csv_row(material, rank))
    path.write_text(output.getvalue(), encoding="utf-8")
    return path


def write_published_index(config: AppConfig, items: list[ApprovedItem], run_date: date) -> Path:
    ensure_storage_dirs(config)
    path = _published_index_path(config)
    payload = _load_json(path, {"schema_version": 1, "dates": {}})
    payload["schema_version"] = 1
    payload["updated_at"] = _utc_now()
    dates = payload.setdefault("dates", {})
    records = []
    for rank, item in enumerate(items, start=1):
        material = item.material
        records.append(
            {
                "rank": rank,
                "key": item.key,
                "source": item.source,
                "item_type": item.item_type,
                "title": item.title,
                "url": item.url,
                "tags": material.tags,
                "quota_group": material.quota_group,
                "score": material.score,
            }
        )
    dates[run_date.isoformat()] = records
    _write_json(path, payload)
    return path


def load_published_index(config: AppConfig) -> dict[str, Any]:
    ensure_storage_dirs(config)
    return _load_json(_published_index_path(config), {"schema_version": 1, "dates": {}})


def resolve_published_item(config: AppConfig, run_date: str, rank: int) -> tuple[str, dict[str, Any]]:
    payload = load_published_index(config)
    dates = payload.get("dates", {}) or {}
    actual_date = run_date
    if run_date == "today":
        actual_date = date.today().isoformat()
    elif run_date == "latest":
        if not dates:
            raise ValueError("No published reports found")
        actual_date = sorted(dates)[-1]
    items = dates.get(actual_date) or []
    for item in items:
        if int(item.get("rank", 0)) == rank:
            return actual_date, item
    raise ValueError(f"No published item for {actual_date} rank {rank}")


def load_feedback_events(config: AppConfig) -> list[FeedbackEvent]:
    ensure_storage_dirs(config)
    payload = _load_json(_feedback_events_path(config), {"schema_version": 1, "events": []})
    events: list[FeedbackEvent] = []
    for raw in payload.get("events", []):
        try:
            events.append(FeedbackEvent.from_dict(raw))
        except TypeError:
            continue
    return events


def list_feedback_events(config: AppConfig, status: str | None = None) -> list[FeedbackEvent]:
    events = load_feedback_events(config)
    if status is None:
        return events
    return [event for event in events if event.status == status]


def append_feedback_event(config: AppConfig, event: FeedbackEvent) -> Path:
    ensure_storage_dirs(config)
    path = _feedback_events_path(config)
    payload = _load_json(path, {"schema_version": 3, "events": []})
    payload["schema_version"] = 3
    payload["updated_at"] = _utc_now()
    payload.setdefault("events", []).append(event.to_dict())
    _write_json(path, payload)
    return path


def upsert_feedback_event(config: AppConfig, event: FeedbackEvent) -> Path:
    ensure_storage_dirs(config)
    path = _feedback_events_path(config)
    payload = _load_json(path, {"schema_version": 3, "events": []})
    events = payload.setdefault("events", [])
    key = _feedback_identity(event.to_dict())
    for index, raw in enumerate(events):
        if _feedback_identity(raw) == key:
            existing = FeedbackEvent.from_dict(raw)
            updated = event.to_dict()
            updated["event_id"] = existing.event_id
            updated["created_at"] = existing.created_at
            updated["status"] = existing.status
            updated["status_reason"] = existing.status_reason
            updated["status_updated_at"] = existing.status_updated_at
            updated["superseded_by_event_id"] = existing.superseded_by_event_id
            events[index] = updated
            break
    else:
        events.append(event.to_dict())
    payload["schema_version"] = 3
    payload["updated_at"] = _utc_now()
    _write_json(path, payload)
    return path


def set_feedback_event_status(config: AppConfig, event_id: str, status: str, reason: str | None = None) -> Path:
    if status not in {"active", "test", "revoked"}:
        raise ValueError(f"Unsupported feedback status: {status}")
    ensure_storage_dirs(config)
    path = _feedback_events_path(config)
    payload = _load_json(path, {"schema_version": 3, "events": []})
    for raw in payload.get("events", []):
        if raw.get("event_id") == event_id:
            raw["status"] = status
            raw["status_reason"] = reason
            raw["status_updated_at"] = _utc_now()
            payload["schema_version"] = 3
            payload["updated_at"] = _utc_now()
            _write_json(path, payload)
            return path
    raise ValueError(f"Feedback event not found: {event_id}")


def _feedback_identity(raw: dict[str, Any]) -> tuple[str, str, int, str] | tuple[str, str]:
    origin = str(raw.get("origin") or "cli")
    external_id = raw.get("external_id")
    if external_id:
        return (origin, str(external_id), int(raw.get("rank", 0)), str(raw.get("signal", "")))
    fingerprint = raw.get("fingerprint")
    if fingerprint:
        return ("fingerprint", str(fingerprint))
    return ("event_id", str(raw.get("event_id", "")))


def load_feedback_suggestions(config: AppConfig) -> list[dict[str, Any]]:
    ensure_storage_dirs(config)
    payload = _load_json(_feedback_suggestions_path(config), {"schema_version": 1, "suggestions": []})
    return [item for item in payload.get("suggestions", []) if isinstance(item, dict)]


def write_feedback_suggestions(config: AppConfig, suggestions: list[dict[str, Any]]) -> Path:
    ensure_storage_dirs(config)
    path = _feedback_suggestions_path(config)
    _write_json(path, {"schema_version": 1, "updated_at": _utc_now(), "suggestions": suggestions})
    return path


def upsert_feedback_suggestions(config: AppConfig, suggestions: list[dict[str, Any]]) -> Path:
    existing = load_feedback_suggestions(config)
    identities = {_suggestion_identity(item): index for index, item in enumerate(existing)}
    for suggestion in suggestions:
        identity = _suggestion_identity(suggestion)
        if identity in identities:
            continue
        existing.append(suggestion)
    return write_feedback_suggestions(config, existing)


def set_feedback_suggestion_status(config: AppConfig, suggestion_id: str, status: str, reason: str | None = None) -> Path:
    if status not in {"pending", "applied", "rejected"}:
        raise ValueError(f"Unsupported suggestion status: {status}")
    suggestions = load_feedback_suggestions(config)
    for suggestion in suggestions:
        if suggestion.get("id") == suggestion_id:
            suggestion["status"] = status
            suggestion["status_reason"] = reason
            now = _utc_now()
            suggestion["updated_at"] = now
            if status == "applied":
                suggestion["applied_at"] = now
            if status == "rejected":
                suggestion["rejected_at"] = now
            return write_feedback_suggestions(config, suggestions)
    raise ValueError(f"Feedback suggestion not found: {suggestion_id}")


def _suggestion_identity(suggestion: dict[str, Any]) -> tuple[str, str]:
    return (str(suggestion.get("type") or ""), str(suggestion.get("value") or ""))


def _merge_evidence(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {"sources": {**((left or {}).get("sources", {}) or {}), **((right or {}).get("sources", {}) or {})}}


def load_health_report(config: AppConfig) -> dict[str, Any]:
    ensure_storage_dirs(config)
    return _load_json(_health_state_path(config), {"schema_version": 1, "history": []})


def write_health_report(config: AppConfig, report: dict[str, Any]) -> Path:
    ensure_storage_dirs(config)
    path = _health_state_path(config)
    _write_json(path, report)
    return path


def load_feishu_delivery_state(config: AppConfig) -> dict[str, Any]:
    ensure_storage_dirs(config)
    return _load_json(_feishu_delivery_path(config), {"schema_version": 1, "weeks": {}})


def write_feishu_delivery_state(config: AppConfig, state: dict[str, Any]) -> Path:
    ensure_storage_dirs(config)
    state["schema_version"] = 1
    state["updated_at"] = _utc_now()
    path = _feishu_delivery_path(config)
    _write_json(path, state)
    return path


def write_run_log(config: AppConfig, run_date: date, status: RunStatus, dry_run: bool) -> Path:
    ensure_storage_dirs(config)
    suffix = ".dry-run" if dry_run else ""
    path = config.logs_dir / f"run-{run_date.isoformat()}{suffix}.log"
    payload = {"date": run_date.isoformat(), "dry_run": dry_run, "status": status.to_dict()}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def weekly_report_path(config: AppConfig, run_date: date) -> Path:
    year, week, _ = run_date.isocalendar()
    return config.reports_dir / f"daily-agent-{year}-W{week:02d}.md"


def weekly_html_report_path(config: AppConfig, run_date: date) -> Path:
    year, week, _ = run_date.isocalendar()
    return config.reports_dir / f"daily-agent-{year}-W{week:02d}.html"


def daily_report_path(config: AppConfig, run_date: date) -> Path:
    return config.reports_dir / f"daily-agent-{run_date.isoformat()}.md"


def daily_html_report_path(config: AppConfig, run_date: date) -> Path:
    return config.reports_dir / f"daily-agent-{run_date.isoformat()}.html"


def write_daily_report(config: AppConfig, run_date: date, daily_markdown: str) -> Path:
    ensure_storage_dirs(config)
    path = daily_report_path(config, run_date)
    path.write_text(daily_markdown.rstrip() + "\n", encoding="utf-8")
    return path


def write_daily_html_report(config: AppConfig, run_date: date, daily_html: str) -> Path:
    ensure_storage_dirs(config)
    path = daily_html_report_path(config, run_date)
    title = f"Daily Agent 日报｜{run_date.isoformat()}"
    body = f'<article class="daily-report" data-run-date="{run_date.isoformat()}">\n{daily_html.rstrip()}\n</article>'
    path.write_text(_html_document(title, title, body), encoding="utf-8")
    return path


def write_weekly_report(config: AppConfig, run_date: date, daily_markdown: str) -> Path:
    ensure_storage_dirs(config)
    path = weekly_report_path(config, run_date)
    marker = _date_marker(run_date)
    end_marker = _date_end_marker(run_date)
    block = f"{marker}\n{daily_markdown.rstrip()}\n{end_marker}\n\n"
    if path.exists():
        content = path.read_text(encoding="utf-8")
        content = _remove_existing_date_block(content, run_date)
        title = _weekly_title(run_date)
        if content.startswith(title):
            rest = content[len(title):].lstrip("\n")
            new_content = f"{title}\n\n{block}{rest}"
        else:
            new_content = f"{title}\n\n{block}{content.strip()}\n"
    else:
        new_content = f"{_weekly_title(run_date)}\n\n{block}"
    path.write_text(new_content.rstrip() + "\n", encoding="utf-8")
    return path


def write_weekly_html_report(config: AppConfig, run_date: date, daily_html: str) -> Path:
    ensure_storage_dirs(config)
    path = weekly_html_report_path(config, run_date)
    marker = _date_marker(run_date)
    end_marker = _date_end_marker(run_date)
    block = f'{marker}\n<article class="daily-report" data-run-date="{run_date.isoformat()}">\n{daily_html.rstrip()}\n</article>\n{end_marker}\n\n'
    if path.exists():
        content = path.read_text(encoding="utf-8")
        body = _html_body_content(_remove_existing_date_block(content, run_date))
        new_body = f"{block}{body}".rstrip()
    else:
        new_body = block.rstrip()
    path.write_text(_weekly_html_document(run_date, new_body), encoding="utf-8")
    return path


def cleanup_retention(config: AppConfig, today: date | None = None) -> None:
    today = today or date.today()
    selected_keep_weeks = int(config.delivery.get("retention", {}).get("selected_keep_weeks", 2))
    markdown_keep_days = int(config.delivery.get("retention", {}).get("weekly_markdown_keep_days", 30))
    html_keep_days = int(config.delivery.get("retention", {}).get("weekly_html_keep_days", markdown_keep_days))
    logs_keep_days = int(config.delivery.get("retention", {}).get("logs_keep_days", 7))

    selected_cutoff = today - timedelta(days=selected_keep_weeks * 7)
    _delete_old_files(config.selected_dir.glob("selected-*.json"), selected_cutoff)
    _delete_old_files(config.selected_dir.glob("selected-*.bib"), selected_cutoff)
    _delete_old_files(config.selected_dir.glob("selected-*.ris"), selected_cutoff)
    _delete_old_files(config.selected_dir.glob("selected-*.csv"), selected_cutoff)
    _delete_old_files(config.selected_dir.glob("selected-*.xml"), selected_cutoff)
    pdf_cache_dir = config.root / str((config.sources.get("pdf_cache", {}) or {}).get("output_dir") or "data/pdfs")
    _delete_old_date_dirs(pdf_cache_dir.glob("????-??-??"), selected_cutoff)
    markdown_cutoff = today - timedelta(days=markdown_keep_days + 7)
    _delete_old_files(config.reports_dir.glob("daily-agent-*.md"), markdown_cutoff)
    html_cutoff = today - timedelta(days=html_keep_days + 7)
    _delete_old_files(config.reports_dir.glob("daily-agent-*.html"), html_cutoff)
    logs_cutoff = today - timedelta(days=logs_keep_days)
    _delete_old_files(config.logs_dir.glob("*.log"), logs_cutoff)

    repeat_days = int(config.sources.get("selection", {}).get("repeat_suppression_days", 30))
    cutoff = today - timedelta(days=repeat_days)
    records = _load_history_index(config, cutoff)
    _write_history_index(config, records.values(), today)


def _selected_record_from_any(item: DigestItem | MaterialRecord | ApprovedItem, rank: int | None = None) -> SelectedRecord:
    if isinstance(item, ApprovedItem):
        return SelectedRecord.from_material(item.material, rank=rank)
    if isinstance(item, MaterialRecord):
        return SelectedRecord.from_material(item, rank=rank)
    return SelectedRecord.from_item(item, rank=rank)


def _material_from_any(item: DigestItem | MaterialRecord | ApprovedItem) -> MaterialRecord:
    if isinstance(item, ApprovedItem):
        return item.material
    if isinstance(item, MaterialRecord):
        return item
    return MaterialRecord.from_item(item)


def _bibtex_entry(material: MaterialRecord, used_keys: set[str]) -> str:
    fields: list[tuple[str, str]] = [("title", material.title)]
    if material.authors:
        fields.append(("author", " and ".join(material.authors)))
    venue = str((material.raw or {}).get("venue") or "").strip()
    if venue:
        fields.append(("journal", venue))
    year = _bibtex_year(material)
    if year:
        fields.append(("year", year))
    if material.doi:
        fields.append(("doi", material.doi))
    if material.url:
        fields.append(("url", material.url))
    arxiv_id = material.source_aliases.get("arxiv") or ((material.raw or {}).get("arxiv_id"))
    if arxiv_id:
        fields.append(("eprint", str(arxiv_id)))
        fields.append(("archivePrefix", "arXiv"))
    key = _bibtex_key(material, year, used_keys)
    lines = [f"@article{{{key},"]
    for index, (name, value) in enumerate(fields):
        suffix = "," if index < len(fields) - 1 else ""
        lines.append(f"  {name} = {{{_bibtex_escape(value)}}}{suffix}")
    lines.append("}")
    return "\n".join(lines)


def _ris_entry(material: MaterialRecord) -> str:
    lines = ["TY  - JOUR", f"TI  - {_ris_value(material.title)}"]
    for author in material.authors:
        lines.append(f"AU  - {_ris_value(author)}")
    year = _bibtex_year(material)
    if year:
        lines.append(f"PY  - {year}")
    venue = str((material.raw or {}).get("venue") or "").strip()
    if venue:
        lines.append(f"JO  - {_ris_value(venue)}")
    if material.doi:
        lines.append(f"DO  - {_ris_value(material.doi)}")
    if material.url:
        lines.append(f"UR  - {_ris_value(material.url)}")
    lines.append("ER  -")
    return "\n".join(lines)


def _endnote_record(material: MaterialRecord) -> ET.Element:
    record = ET.Element("record")
    ref_type = ET.SubElement(record, "ref-type", {"name": "Journal Article"})
    ref_type.text = "17"
    contributors = ET.SubElement(record, "contributors")
    authors = ET.SubElement(contributors, "authors")
    for author in material.authors:
        ET.SubElement(authors, "author").text = author
    titles = ET.SubElement(record, "titles")
    ET.SubElement(titles, "title").text = material.title
    venue = str((material.raw or {}).get("venue") or "").strip()
    if venue:
        ET.SubElement(titles, "secondary-title").text = venue
    year = _bibtex_year(material)
    if year:
        dates = ET.SubElement(record, "dates")
        ET.SubElement(dates, "year").text = year
    if material.doi:
        ET.SubElement(record, "electronic-resource-num").text = material.doi
    if material.url:
        urls = ET.SubElement(record, "urls")
        ET.SubElement(urls, "related-urls").text = material.url
    return record


def _csv_row(material: MaterialRecord, rank: int) -> dict[str, str]:
    return {
        "rank": str(rank),
        "item_type": material.item_type,
        "source": material.source,
        "title": material.title,
        "authors": "; ".join(material.authors),
        "doi": material.doi or "",
        "url": material.url,
        "pdf_url": material.pdf_url or "",
        "score": f"{material.score:g}",
        "tags": "; ".join(material.tags),
    }


def _bibtex_year(material: MaterialRecord) -> str:
    candidates = [
        material.source_updated_at,
        (material.raw or {}).get("published_at"),
        (material.raw or {}).get("publication_date"),
        (material.raw or {}).get("year"),
    ]
    for candidate in candidates:
        match = re.search(r"(19|20)\d{2}", str(candidate or ""))
        if match:
            return match.group(0)
    return ""


def _bibtex_key(material: MaterialRecord, year: str, used_keys: set[str]) -> str:
    author_token = _bibtex_author_token(material)
    title_token = "".join(_slug_words(material.title)[:2]) or "paper"
    base = f"{author_token}{year or 'nodate'}{title_token}"
    key = base
    counter = 2
    while key in used_keys:
        key = f"{base}{counter}"
        counter += 1
    used_keys.add(key)
    return key


def _bibtex_author_token(material: MaterialRecord) -> str:
    if material.authors:
        words = _slug_words(material.authors[0])
        if words:
            return words[-1]
    return _slug_words(material.source)[0] if _slug_words(material.source) else "paper"


def _slug_words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def _bibtex_escape(value: str) -> str:
    replacements = {
        "\\": r"\\",
        "{": r"\{",
        "}": r"\}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
    }
    return "".join(replacements.get(char, char) for char in str(value))


def _ris_value(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_json(path: Path, default):
    if not path.exists():
        return default.copy() if isinstance(default, dict) else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default.copy() if isinstance(default, dict) else default


def _has_update_label(record: MaterialRecord) -> bool:
    return record.update_label in {"version_update", "major_update"}


def _latest_published_date(record: MaterialRecord) -> date | None:
    dates = [_parse_date(value) for value in record.published_dates]
    return max((value for value in dates if value), default=None)


def _is_recent_repeat_without_update(record: MaterialRecord, run_date: date, repeat_days: int) -> bool:
    if not record.published_dates or _has_update_label(record):
        return False
    latest_published = _latest_published_date(record)
    return bool(latest_published and latest_published >= run_date - timedelta(days=repeat_days))


def _iter_selected_records(paths: Iterable[Path]) -> Iterable[SelectedRecord]:
    for path in sorted(paths):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("dry_run"):
            continue
        for raw in payload.get("selected", []):
            try:
                yield SelectedRecord.from_dict(raw)
            except TypeError:
                continue


def _history_index_path(config: AppConfig) -> Path:
    return config.state_dir / HISTORY_INDEX_NAME


def _published_index_path(config: AppConfig) -> Path:
    return config.state_dir / PUBLISHED_INDEX_NAME


def _feedback_events_path(config: AppConfig) -> Path:
    return config.state_dir / FEEDBACK_EVENTS_NAME


def _feedback_suggestions_path(config: AppConfig) -> Path:
    return config.state_dir / FEEDBACK_SUGGESTIONS_NAME


def _health_state_path(config: AppConfig) -> Path:
    return config.state_dir / HEALTH_STATE_NAME


def _feishu_delivery_path(config: AppConfig) -> Path:
    return config.state_dir / FEISHU_DELIVERY_NAME


def _load_history_index(config: AppConfig, cutoff: date) -> dict[str, SelectedRecord]:
    ensure_storage_dirs(config)
    path = _history_index_path(config)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    records: dict[str, SelectedRecord] = {}
    for raw in payload.get("records", []):
        try:
            record = SelectedRecord.from_dict(raw)
        except TypeError:
            continue
        selected_at = _parse_date(record.selected_at)
        if selected_at and selected_at >= cutoff:
            records[record.key] = record
    return records


def _write_history_index(config: AppConfig, records: Iterable[SelectedRecord], today: date) -> None:
    ensure_storage_dirs(config)
    cutoff = today - timedelta(days=int(config.sources.get("selection", {}).get("repeat_suppression_days", 30)))
    deduped: dict[str, SelectedRecord] = {}
    for record in records:
        selected_at = _parse_date(record.selected_at)
        if selected_at and selected_at >= cutoff:
            deduped[record.key] = record
    payload = {
        "updated_at": _utc_now(),
        "repeat_suppression_days": int(config.sources.get("selection", {}).get("repeat_suppression_days", 30)),
        "records": [record.to_dict() for record in sorted(deduped.values(), key=lambda item: item.selected_at)],
    }
    _history_index_path(config).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _delete_old_files(paths: Iterable[Path], cutoff: date) -> None:
    for path in paths:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date()
        except FileNotFoundError:
            continue
        if mtime < cutoff:
            path.unlink(missing_ok=True)


def _delete_old_date_dirs(paths: Iterable[Path], cutoff: date) -> None:
    for path in paths:
        if not path.is_dir():
            continue
        dir_date = _parse_date(path.name)
        if dir_date and dir_date < cutoff:
            shutil.rmtree(path, ignore_errors=True)


def _weekly_title(run_date: date) -> str:
    year, week, _ = run_date.isocalendar()
    return f"# Daily Agent 日报｜{year} 第 {week:02d} 周"


def _weekly_html_title(run_date: date) -> str:
    year, week, _ = run_date.isocalendar()
    return f"Daily Agent 日报｜{year} 第 {week:02d} 周"


def _weekly_html_document(run_date: date, body: str) -> str:
    title = _weekly_html_title(run_date)
    return _html_document(title, title, body)


def _html_document(title: str, header_title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{ color-scheme: light; --bg: #f6f7fb; --card: #ffffff; --text: #20242a; --muted: #657083; --border: #e3e7ef; --accent: #315efb; --soft: #f9fafb; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.65; }}
    main {{ max-width: 960px; margin: 0 auto; padding: 32px 18px 56px; }}
    .weekly-title, .daily-report, section {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; }}
    .weekly-title {{ padding: 24px; margin: 0 0 18px; }}
    .daily-report {{ padding: 24px; margin: 18px 0; }}
    section {{ padding: 18px; margin: 16px 0; }}
    h1, h2, h3 {{ line-height: 1.25; }}
    h1 {{ margin-top: 0; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    dl {{ display: grid; grid-template-columns: minmax(120px, 180px) 1fr; gap: 8px 16px; }}
    dt {{ color: var(--muted); font-weight: 700; }}
    dd {{ margin: 0; min-width: 0; overflow-wrap: anywhere; }}
    .report-item {{ border-top: 1px solid var(--border); padding-top: 14px; margin-top: 14px; }}
    .report-item:first-of-type {{ border-top: 0; padding-top: 0; }}
    .feedback-panel {{ display: flex; flex-wrap: wrap; align-items: center; gap: 10px 14px; max-width: 100%; }}
    .feedback-copy {{ flex: 1 1 320px; min-width: min(320px, 100%); display: flex; flex-direction: column; gap: 2px; overflow-wrap: anywhere; }}
    .feedback-title {{ font-weight: 700; color: var(--text); }}
    .feedback-note {{ color: var(--muted); font-size: 0.93rem; }}
    .feedback-actions {{ display: flex; flex: 0 0 auto; flex-wrap: wrap; gap: 8px; align-items: center; margin: 0; }}
    .feedback-actions button {{ appearance: none; border: 1px solid var(--border); border-radius: 6px; background: var(--soft); color: var(--text); padding: 4px 12px; font: inherit; cursor: pointer; min-height: 34px; }}
    .feedback-actions button:hover {{ border-color: var(--accent); color: var(--accent); }}
    @media (max-width: 680px) {{ dl {{ display: block; }} dt {{ margin-top: 10px; }} .feedback-actions {{ width: 100%; }} .feedback-actions button {{ flex: 1 1 120px; }} }}
  </style>
</head>
<body>
<main>
<header class="weekly-title"><h1>{escape(header_title)}</h1></header>
{body.rstrip()}
</main>
</body>
</html>
""".rstrip() + "\n"


def _html_body_content(content: str) -> str:
    start = content.find("<main>")
    end = content.rfind("</main>")
    if start == -1 or end == -1:
        return content.strip()
    body = content[start + len("<main>"):end].strip()
    header_end = body.find("</header>")
    if header_end != -1:
        body = body[header_end + len("</header>"):].strip()
    return body


def _date_marker(run_date: date) -> str:
    return f"{DATE_MARKER_PREFIX}{run_date.isoformat()} -->"


def _date_end_marker(run_date: date) -> str:
    return f"<!-- /daily-agent-date:{run_date.isoformat()} -->"


def _remove_existing_date_block(content: str, run_date: date) -> str:
    start = _date_marker(run_date)
    end = _date_end_marker(run_date)
    start_index = content.find(start)
    if start_index == -1:
        return content
    end_index = content.find(end, start_index)
    if end_index == -1:
        return content[:start_index].rstrip() + "\n"
    end_index += len(end)
    return (content[:start_index] + content[end_index:]).lstrip("\n")


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
