from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from daily_agent.editorial import GENERIC_PHRASES
from daily_agent.models import ApprovedItem, EditorialDraft, EditorialReview, MaterialRecord, RunStatus, utc_now_iso

DEFAULT_THRESHOLDS = {
    "min_approved_items": 3,
    "max_not_stated_total": 10,
    "max_generic_phrase_total": 0,
    "recent_history_limit": 7,
    "repeated_problem_threshold": 2,
}


def evaluate_run_health(
    config,
    run_date: date,
    dry_run: bool,
    status: RunStatus,
    approved: list[ApprovedItem],
    shortlist: list[MaterialRecord],
    drafts: list[EditorialDraft],
    reviews: list[EditorialReview],
    weekly_report_path: Path,
    weekly_html_path: Path,
    selected_path: Path,
    editorial_path: Path,
    previous_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    thresholds = _thresholds(config)
    artifacts = summarize_artifacts(weekly_report_path, weekly_html_path, selected_path, editorial_path)
    duplicate_approved_keys = len(approved) - len({item.key for item in approved})
    review_failures = sum(1 for review in reviews if review.verdict == "FAIL")
    not_stated_count = count_not_stated([draft.draft_fields for draft in drafts]) + count_not_stated([item.final_fields for item in approved])
    generic_phrase_count = count_generic_phrases([draft.draft_fields for draft in drafts]) + count_generic_phrases([item.final_fields for item in approved])
    source_failures = sum(1 for source in status.sources if not source.ok)
    source_coverage_count = len({source.name.split("/", 1)[0] for source in status.sources if source.ok and source.item_count > 0})
    approved_source_diversity = _approved_source_diversity(approved)
    delivery = summarize_delivery(status.delivery)
    missing_artifact_count = sum(1 for ok in artifacts.values() if not ok)
    update_signal_count = sum(1 for item in approved if item.material.update_label in {"version_update", "major_update"})
    consecutive_problem_runs = _consecutive_problem_runs(previous_health, thresholds["repeated_problem_threshold"])

    summary = {
        "approved_count": len(approved),
        "shortlist_count": len(shortlist),
        "draft_count": len(drafts),
        "review_failures": review_failures,
        "source_failures": source_failures,
        "source_coverage_count": source_coverage_count,
        "approved_source_diversity": approved_source_diversity,
        "not_stated_count": not_stated_count,
        "generic_phrase_count": generic_phrase_count,
        "feishu_fallback_used": bool(delivery.get("fallback_used") or (delivery.get("requested_mode") == "feishu" and delivery.get("final_mode") not in {"feishu", None})),
        "missing_artifact_count": missing_artifact_count,
        "duplicate_approved_keys": duplicate_approved_keys,
        "update_signal_count": update_signal_count,
        "consecutive_problem_runs": consecutive_problem_runs,
    }
    checks = _checks(summary, delivery, artifacts, thresholds)
    overall = _overall(checks)
    current = {
        "generated_at": utc_now_iso(),
        "run_date": run_date.isoformat(),
        "dry_run": dry_run,
        "overall": overall,
        "summary": summary,
        "checks": checks,
        "signals": {
            "sources": summarize_sources(status),
            "delivery": delivery,
            "artifacts": artifacts,
        },
    }
    history = _health_history(previous_health, current, thresholds["recent_history_limit"])
    return {"schema_version": 1, "current": current, "history": history}


def count_not_stated(value: Any) -> int:
    if isinstance(value, dict):
        return sum(count_not_stated(item) for item in value.values())
    if isinstance(value, list):
        return sum(count_not_stated(item) for item in value)
    if isinstance(value, str):
        return 1 if value.strip() == "not_stated" else 0
    return 0


def count_generic_phrases(value: Any, phrases: list[str] | None = None) -> int:
    phrases = phrases or GENERIC_PHRASES
    if isinstance(value, dict):
        return sum(count_generic_phrases(item, phrases) for item in value.values())
    if isinstance(value, list):
        return sum(count_generic_phrases(item, phrases) for item in value)
    if isinstance(value, str):
        return sum(1 for phrase in phrases if phrase in value)
    return 0


def _approved_source_diversity(approved: list[ApprovedItem]) -> int:
    sources: set[str] = set()
    for item in approved:
        aliases = item.material.source_aliases or {}
        if aliases:
            sources.update(aliases)
        else:
            sources.add(item.source)
    return len(sources)


def build_issue(id: str, severity: str, status: str, message: str, metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"id": id, "severity": severity, "status": status, "message": message}
    if metrics is not None:
        payload["metrics"] = metrics
    return payload


def summarize_sources(status: RunStatus) -> list[dict[str, Any]]:
    return [
        {
            "name": source.name,
            "ok": source.ok,
            "item_count": source.item_count,
            "retries": source.retries,
            "skipped": source.skipped,
            "error_present": bool(source.error),
        }
        for source in status.sources
    ]


def summarize_delivery(delivery) -> dict[str, Any]:
    if not delivery:
        return {"requested_mode": None, "final_mode": None, "ok": None, "fallback_used": False, "document_url_present": False}
    return {
        "requested_mode": delivery.requested_mode,
        "final_mode": delivery.final_mode,
        "ok": delivery.ok,
        "fallback_used": delivery.fallback_used,
        "document_url_present": bool(delivery.document_url),
    }


def summarize_artifacts(weekly_report_path: Path, weekly_html_path: Path, selected_path: Path, editorial_path: Path) -> dict[str, bool]:
    return {
        "weekly_report": weekly_report_path.exists(),
        "weekly_html_report": weekly_html_path.exists(),
        "selected_json": selected_path.exists(),
        "shortlist": (editorial_path / "shortlist.json").exists(),
        "writer_draft": (editorial_path / "writer_draft.json").exists(),
        "editor_review": (editorial_path / "editor_review.json").exists(),
        "approval": (editorial_path / "approval.json").exists(),
    }


def _thresholds(config) -> dict[str, int]:
    values = DEFAULT_THRESHOLDS.copy()
    health = (config.delivery.get("health", {}) if getattr(config, "delivery", None) else {}) or {}
    for key in values:
        if key in health:
            values[key] = int(health[key])
    return values


def _checks(summary: dict[str, Any], delivery: dict[str, Any], artifacts: dict[str, bool], thresholds: dict[str, int]) -> list[dict[str, Any]]:
    checks = []
    checks.append(build_issue("approved_count_zero", "error", "fail" if summary["approved_count"] == 0 else "pass", "No approved items", {"actual": summary["approved_count"]}))
    checks.append(build_issue("approved_count_low", "warning", "fail" if 0 < summary["approved_count"] < thresholds["min_approved_items"] else "pass", "Approved item count is below target", {"actual": summary["approved_count"], "min": thresholds["min_approved_items"]}))
    checks.append(build_issue("source_failures", "warning", "fail" if summary["source_failures"] else "pass", "One or more sources failed", {"actual": summary["source_failures"]}))
    checks.append(build_issue("delivery_failed", "error", "fail" if delivery.get("ok") is False else "pass", "Delivery failed", {"ok": delivery.get("ok")}))
    checks.append(build_issue("feishu_fallback", "warning", "fail" if summary["feishu_fallback_used"] else "pass", "Feishu delivery used fallback", {"fallback_used": summary["feishu_fallback_used"]}))
    missing = [key for key, ok in artifacts.items() if not ok]
    checks.append(build_issue("missing_artifacts", "error", "fail" if missing else "pass", "One or more run artifacts are missing", {"missing": missing}))
    checks.append(build_issue("review_failures", "warning", "fail" if summary["review_failures"] else "pass", "One or more drafts failed review", {"actual": summary["review_failures"]}))
    checks.append(build_issue("not_stated_total", "warning", "fail" if summary["not_stated_count"] > thresholds["max_not_stated_total"] else "pass", "Too many not_stated fields", {"actual": summary["not_stated_count"], "max": thresholds["max_not_stated_total"]}))
    checks.append(build_issue("generic_phrase_total", "warning", "fail" if summary["generic_phrase_count"] > thresholds["max_generic_phrase_total"] else "pass", "Generic phrases detected", {"actual": summary["generic_phrase_count"], "max": thresholds["max_generic_phrase_total"]}))
    checks.append(build_issue("duplicate_approved_keys", "warning", "fail" if summary["duplicate_approved_keys"] else "pass", "Duplicate approved keys detected", {"actual": summary["duplicate_approved_keys"]}))
    checks.append(build_issue("consecutive_problem_runs", "warning", "fail" if summary["consecutive_problem_runs"] >= thresholds["repeated_problem_threshold"] else "pass", "Repeated warning or failed runs detected", {"actual": summary["consecutive_problem_runs"], "threshold": thresholds["repeated_problem_threshold"]}))
    return checks


def _overall(checks: list[dict[str, Any]]) -> str:
    if any(check["status"] == "fail" and check["severity"] == "error" for check in checks):
        return "failed"
    if any(check["status"] == "fail" for check in checks):
        return "warning"
    return "healthy"


def _consecutive_problem_runs(previous_health: dict[str, Any] | None, threshold: int) -> int:
    count = 0
    for item in reversed((previous_health or {}).get("history", []) or []):
        if item.get("overall") in {"warning", "failed"}:
            count += 1
            continue
        break
    return count


def _health_history(previous_health: dict[str, Any] | None, current: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    previous = list((previous_health or {}).get("history", []) or [])
    previous.append(
        {
            "run_date": current["run_date"],
            "dry_run": current["dry_run"],
            "overall": current["overall"],
            "issue_count": sum(1 for check in current["checks"] if check["status"] == "fail"),
        }
    )
    return previous[-limit:]
