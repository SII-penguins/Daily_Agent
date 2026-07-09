from __future__ import annotations

import argparse
import contextlib
from datetime import date, datetime, timezone
import os
from pathlib import Path

from daily_agent.config import load_config
from daily_agent.connectors.google_scholar import SCHOLARLY_RUNTIME_ENV
from daily_agent.feedback.feishu_comments import sync_feishu_feedback
from daily_agent.feedback.ingest import build_feedback_event, record_feedback_text
from daily_agent.feedback.profile import build_feedback_profile, render_feedback_profile
from daily_agent.feedback.server import DEFAULT_FEEDBACK_HOST, DEFAULT_FEEDBACK_PORT, feedback_form_action, serve_feedback
from daily_agent.feedback.suggestions import apply_feedback_suggestion, format_suggestion, generate_feedback_suggestions, process_suggestion_response_text, reject_feedback_suggestion, save_feedback_suggestions
from daily_agent.full_profile import enforce_full_profile, render_full_profile_result
from daily_agent.pipeline import run_pipeline
from daily_agent.preview import DEFAULT_REPORT_PORT, serve_preview, start_preview_server
from daily_agent.quality import render_quality_check, run_quality_check
from daily_agent.scheduling import build_schedule_preview
from daily_agent.secrets import render_external_secrets_template, render_missing_external_secrets_template, render_secrets_status, write_external_secrets_template, write_missing_external_secrets_template
from daily_agent.source_check import render_source_check, run_source_check
from daily_agent.storage import append_feedback_event, load_feedback_events, load_feedback_suggestions, load_material_library, resolve_published_item, set_feedback_event_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Daily Agent research digest")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run daily digest pipeline")
    run_parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    run_parser.add_argument("--date", default="today", help="YYYY-MM-DD or today")
    run_parser.add_argument("--dry-run", action="store_true", default=False)
    run_parser.add_argument("--send", choices=["local", "cc-connect", "cc_connect", "feishu"], default="local")
    run_parser.set_defaults(llm=True)
    run_parser.add_argument("--llm", dest="llm", action="store_true", help="Use local Claude Code for structured summaries (default)")
    run_parser.add_argument("--no-llm", dest="llm", action="store_false", help="Disable local Claude Code drafting and use rule-based summaries only")
    run_parser.add_argument("--allow-degraded", action="store_true", help="Allow formal external delivery even when the full-quality preflight is not full")
    run_parser.add_argument("--require-full", action="store_true", help="Require a full-quality preflight even for dry-run or local runs")
    run_parser.add_argument("--supervised-scholar-fallback", action="store_true", help="Temporarily enable the scholarly Google Scholar fallback for this supervised run")

    schedule_parser = subparsers.add_parser("schedule", help="Preview scheduled run setup")
    schedule_subparsers = schedule_parser.add_subparsers(dest="schedule_command")
    schedule_preview = schedule_subparsers.add_parser("preview", help="Print scheduler setup without installing it")
    schedule_preview.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    schedule_preview.add_argument("--backend", choices=["cc-connect", "launchd"], default="cc-connect")

    source_parser = subparsers.add_parser("source", help="Inspect source connectivity")
    source_subparsers = source_parser.add_subparsers(dest="source_command")
    source_check = source_subparsers.add_parser("check", help="Check enabled sources without writing reports")
    source_check.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    source_check.add_argument("--date", default="today", help="YYYY-MM-DD or today")
    source_check.add_argument("--window-days", type=int, default=7)
    source_check.add_argument("--sample-limit", type=int, default=3)
    source_check.add_argument("--supervised-scholar-fallback", action="store_true", help="Temporarily enable the scholarly Google Scholar fallback for this supervised check")
    source_check.add_argument("--no-enforce-full", action="store_true", help="Inspect the current config without first repairing the full-quality profile")

    preview_parser = subparsers.add_parser("preview", help="Serve generated reports and feedback buttons")
    preview_subparsers = preview_parser.add_subparsers(dest="preview_command")
    preview_start = preview_subparsers.add_parser("start", help="Start local report preview and feedback services in the background")
    preview_start.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    preview_start.add_argument("--host", default=DEFAULT_FEEDBACK_HOST)
    preview_start.add_argument("--report-port", type=int, default=DEFAULT_REPORT_PORT)
    preview_start.add_argument("--feedback-port", type=int, default=DEFAULT_FEEDBACK_PORT)
    preview_start.add_argument("--report", choices=["latest", "daily", "weekly"], default="latest")
    preview_serve = preview_subparsers.add_parser("serve", help="Run local report preview and feedback services in the foreground")
    preview_serve.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    preview_serve.add_argument("--host", default=DEFAULT_FEEDBACK_HOST)
    preview_serve.add_argument("--report-port", type=int, default=DEFAULT_REPORT_PORT)
    preview_serve.add_argument("--feedback-port", type=int, default=DEFAULT_FEEDBACK_PORT)

    quality_parser = subparsers.add_parser("quality", help="Inspect full-quality runtime readiness")
    quality_subparsers = quality_parser.add_subparsers(dest="quality_command")
    quality_check = quality_subparsers.add_parser("check", help="Check full-text, LLM, source, schedule, and delivery readiness")
    quality_check.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    quality_check.add_argument("--require-full", action="store_true", help="Return a non-zero exit code unless every quality check is full")
    quality_check.add_argument("--supervised-scholar-fallback", action="store_true", help="Report Google Scholar as supervised scholarly fallback when SerpAPI is missing and scholarly is installed")
    quality_check.add_argument("--no-enforce-full", action="store_true", help="Inspect the current config without first repairing the full-quality profile")
    enforce_full = quality_subparsers.add_parser("enforce-full", help="Report or apply the full-quality config profile")
    enforce_full.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    enforce_mode = enforce_full.add_mutually_exclusive_group()
    enforce_mode.add_argument("--dry-run", action="store_true", help="Only report config drift; this is the default")
    enforce_mode.add_argument("--write", action="store_true", help="Write full-quality settings to config files")
    secrets_template = quality_subparsers.add_parser("secrets-template", help="Print or write an external secrets TOML template")
    secrets_template.add_argument("--path", default=None, help="Write the template to this external path instead of printing it")
    secrets_template.add_argument("--force", action="store_true", help="Overwrite --path if it already exists")
    secrets_template.add_argument("--missing-only", action="store_true", help="Include only missing or placeholder full-quality credentials")
    quality_subparsers.add_parser("secrets-status", help="Report required full-quality credential status without printing secret values")

    feedback_parser = subparsers.add_parser("feedback", help="Manage feedback events")
    feedback_subparsers = feedback_parser.add_subparsers(dest="feedback_command")

    feedback_add = feedback_subparsers.add_parser("add", help="Add item feedback")
    feedback_add.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    feedback_add.add_argument("--date", default=None, help="YYYY-MM-DD, today, or latest")
    feedback_add.add_argument("--rank", type=int)
    feedback_add.add_argument("--signal", choices=["like", "dislike"])
    feedback_add.add_argument("--text", help="Natural-language feedback, e.g. 今天第 3 条不行，第 8 条不错")
    feedback_add.add_argument("--keyword", action="append", default=[])
    feedback_add.add_argument("--note")
    feedback_add.add_argument("--channel", choices=["local", "feishu", "cc-connect"], default="local")

    feedback_show = feedback_subparsers.add_parser("show", help="Show recent feedback")
    feedback_show.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    feedback_show.add_argument("--limit", type=int, default=20)

    feedback_sync = feedback_subparsers.add_parser("sync", help="Sync external feedback")
    feedback_sync.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    feedback_sync.add_argument("--source", choices=["feishu"], required=True)
    feedback_sync.add_argument("--week", default="latest", help="latest or ISO week key like 2026-W20")
    feedback_sync.add_argument("--date", default=None, help="YYYY-MM-DD, today, or latest")
    feedback_sync.add_argument("--dry-run", action="store_true")

    feedback_mark_test = feedback_subparsers.add_parser("mark-test", help="Mark feedback event as test")
    feedback_mark_test.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    feedback_mark_test.add_argument("--event-id", required=True)
    feedback_mark_test.add_argument("--reason", default=None)

    feedback_undo = feedback_subparsers.add_parser("undo", help="Revoke feedback event")
    feedback_undo.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    feedback_undo.add_argument("--event-id", required=True)
    feedback_undo.add_argument("--reason", default=None)

    feedback_cleanup = feedback_subparsers.add_parser("cleanup", help="Mark likely test feedback events")
    feedback_cleanup.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    feedback_cleanup.add_argument("--dry-run", action="store_true")

    feedback_explain = feedback_subparsers.add_parser("explain", help="Explain active feedback impact")
    feedback_explain.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    feedback_explain.add_argument("--limit", type=int, default=10)
    feedback_explain.add_argument("--date", default=None)
    feedback_explain.add_argument("--rank", type=int)

    feedback_profile = feedback_subparsers.add_parser("profile", help="Show read-only feedback preference profile")
    feedback_profile.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    feedback_profile.add_argument("--limit", type=int, default=20)
    feedback_profile.add_argument("--include-ignored", action="store_true")

    feedback_suggest = feedback_subparsers.add_parser("suggest", help="Generate pending preference suggestions")
    feedback_suggest.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    feedback_suggest.add_argument("--dry-run", action="store_true")

    feedback_suggestions = feedback_subparsers.add_parser("suggestions", help="List preference suggestions")
    feedback_suggestions.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    feedback_suggestions.add_argument("--status", choices=["pending", "applied", "rejected", "all"], default="pending")

    feedback_apply_suggestion = feedback_subparsers.add_parser("apply-suggestion", help="Apply a pending preference suggestion")
    feedback_apply_suggestion.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    feedback_apply_suggestion.add_argument("--id", required=True)

    feedback_reject_suggestion = feedback_subparsers.add_parser("reject-suggestion", help="Reject a preference suggestion")
    feedback_reject_suggestion.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    feedback_reject_suggestion.add_argument("--id", required=True)
    feedback_reject_suggestion.add_argument("--reason", default=None)

    feedback_respond = feedback_subparsers.add_parser("respond", help="Apply or reject a preference suggestion from text")
    feedback_respond.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    feedback_respond.add_argument("--text", required=True, help="Example: 确认建议 sug_... or 拒绝建议 sug_... 因为 ...")
    feedback_respond.add_argument("--dry-run", action="store_true")

    feedback_serve = feedback_subparsers.add_parser("serve", help="Run local feedback button server")
    feedback_serve.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    feedback_serve.add_argument("--host", default=DEFAULT_FEEDBACK_HOST)
    feedback_serve.add_argument("--port", type=int, default=DEFAULT_FEEDBACK_PORT)

    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    if args.command == "schedule":
        if args.schedule_command == "preview":
            return _schedule_preview(args)
    if args.command == "source":
        if args.source_command == "check":
            return _source_check(args)
    if args.command == "preview":
        if args.preview_command == "start":
            return _preview_start(args)
        if args.preview_command == "serve":
            return _preview_serve(args)
    if args.command == "quality":
        if args.quality_command == "check":
            return _quality_check(args)
        if args.quality_command == "enforce-full":
            return _quality_enforce_full(args)
        if args.quality_command == "secrets-template":
            return _quality_secrets_template(args)
        if args.quality_command == "secrets-status":
            return _quality_secrets_status(args)
    if args.command == "feedback":
        if args.feedback_command == "add":
            return _feedback_add(args)
        if args.feedback_command == "show":
            return _feedback_show(args)
        if args.feedback_command == "sync":
            return _feedback_sync(args)
        if args.feedback_command == "mark-test":
            return _feedback_set_status(args, "test")
        if args.feedback_command == "undo":
            return _feedback_set_status(args, "revoked")
        if args.feedback_command == "cleanup":
            return _feedback_cleanup(args)
        if args.feedback_command == "explain":
            return _feedback_explain(args)
        if args.feedback_command == "profile":
            return _feedback_profile(args)
        if args.feedback_command == "suggest":
            return _feedback_suggest(args)
        if args.feedback_command == "suggestions":
            return _feedback_suggestions(args)
        if args.feedback_command == "apply-suggestion":
            return _feedback_apply_suggestion(args)
        if args.feedback_command == "reject-suggestion":
            return _feedback_reject_suggestion(args)
        if args.feedback_command == "respond":
            return _feedback_respond(args)
        if args.feedback_command == "serve":
            return _feedback_serve(args)
    parser.print_help()
    return 1


def _schedule_preview(args) -> int:
    config = load_config(args.root)
    try:
        preview = build_schedule_preview(config, backend=args.backend)
    except ValueError as exc:
        print(f"Schedule error: {exc}")
        return 1
    print(f"Schedule backend: {preview.backend}")
    print(f"Timezone: {preview.timezone}")
    for job in preview.jobs:
        print(f"- {job.name}: {job.time.cron} ({job.description})")
    print(preview.body)
    return 0


def _source_check(args) -> int:
    with _supervised_scholar_runtime(bool(getattr(args, "supervised_scholar_fallback", False))):
        if not getattr(args, "no_enforce_full", False):
            _enforce_full_profile_for_runtime(args.root)
        config = load_config(args.root)
        run_date = date.today() if args.date == "today" else date.fromisoformat(args.date)
        target_dt = datetime(run_date.year, run_date.month, run_date.day, tzinfo=timezone.utc)
        results = run_source_check(config, target_dt, window_days=args.window_days, sample_limit=args.sample_limit)
        print(render_source_check(results, run_date, args.window_days))
        return 0


def _preview_start(args) -> int:
    config = load_config(args.root)
    try:
        result = start_preview_server(
            config,
            host=args.host,
            report_port=args.report_port,
            feedback_port=args.feedback_port,
            report=args.report,
        )
    except ValueError as exc:
        print(f"Preview error: {exc}")
        return 1
    state = "already running" if result.get("already_running") else f"started pid={result.get('pid')}"
    print(f"Daily Agent preview {state}")
    print(f"HTML report: {result['html_url']}")
    if result.get("markdown_url"):
        print(f"Markdown report: {result['markdown_url']}")
    print(f"Feedback buttons: {result['feedback_url']}")
    return 0


def _preview_serve(args) -> int:
    config = load_config(args.root)
    print(f"Daily Agent preview: http://{args.host}:{args.report_port}/")
    print(f"Feedback buttons: http://{args.host}:{args.feedback_port}/")
    print("Press Ctrl-C to stop.")
    try:
        serve_preview(config, host=args.host, report_port=args.report_port, feedback_port=args.feedback_port)
    except KeyboardInterrupt:
        print("\nDaily Agent preview stopped.")
    return 0


def _quality_check(args) -> int:
    with _supervised_scholar_runtime(bool(getattr(args, "supervised_scholar_fallback", False))):
        if not getattr(args, "no_enforce_full", False):
            _enforce_full_profile_for_runtime(args.root)
        config = load_config(args.root)
        profile = run_quality_check(config)
        print(render_quality_check(profile))
        if args.require_full and profile.overall != "full":
            print(f"Required full-quality profile but got {profile.overall}")
            return 2
        return 0


def _quality_enforce_full(args) -> int:
    result = enforce_full_profile(args.root, write=bool(args.write))
    print(render_full_profile_result(result))
    if result.changed and not result.written:
        return 1
    return 0


def _quality_secrets_template(args) -> int:
    if not args.path:
        print(render_missing_external_secrets_template() if args.missing_only else render_external_secrets_template())
        return 0
    try:
        writer = write_missing_external_secrets_template if args.missing_only else write_external_secrets_template
        path = writer(args.path, force=args.force)
    except FileExistsError:
        print(f"Secrets template already exists: {args.path}. Use --force to overwrite.")
        return 1
    print(f"Secrets template written: {path}")
    print(f"Set DAILY_AGENT_SECRETS_FILE={path} before running full-quality checks.")
    return 0


def _quality_secrets_status(args) -> int:
    print(render_secrets_status())
    return 0


def _enforce_full_profile_for_runtime(root: str) -> None:
    result = enforce_full_profile(root, write=True)
    if result.changed:
        print(render_full_profile_result(result))


def _run(args) -> int:
    with _supervised_scholar_runtime(bool(getattr(args, "supervised_scholar_fallback", False))):
        return _run_with_runtime(args)


def _run_with_runtime(args) -> int:
    run_date = date.today() if args.date == "today" else date.fromisoformat(args.date)
    enforce_result = enforce_full_profile(args.root, write=True)
    if enforce_result.changed:
        print(render_full_profile_result(enforce_result))
    preflight_required = bool(args.require_full or (not args.dry_run and args.send not in {"local"} and not args.allow_degraded))
    if preflight_required:
        config = load_config(args.root)
        profile = run_quality_check(config)
        if profile.overall != "full":
            print(render_quality_check(profile))
            if args.require_full:
                print(f"Full-quality preflight failed: {profile.overall}. Remove --require-full only if you intentionally want a degraded local validation run.")
            else:
                print(f"Full-quality preflight failed: {profile.overall}. Use --allow-degraded only if you intentionally want external delivery anyway.")
            return 2
    result = run_pipeline(
        root=args.root,
        run_date=run_date,
        dry_run=args.dry_run,
        use_llm=args.llm,
        delivery_mode=args.send,
    )
    print(f"Daily Agent report generated: {result.weekly_report_path}")
    if getattr(result, "weekly_html_path", None):
        print(f"HTML report: {result.weekly_html_path}")
    print(f"Selected JSON: {result.selected_path}")
    if getattr(result, "bibtex_path", None):
        print(f"BibTeX export: {result.bibtex_path}")
    if getattr(result, "ris_path", None):
        print(f"RIS export: {result.ris_path}")
    if getattr(result, "csv_path", None):
        print(f"CSV export: {result.csv_path}")
    if getattr(result, "endnote_xml_path", None):
        print(f"EndNote XML export: {result.endnote_xml_path}")
    print(f"Selected items: {len(result.items)}")
    if result.status.delivery:
        print(f"Delivery: {result.status.delivery.final_mode} ({'ok' if result.status.delivery.ok else 'failed'})")
        if result.status.delivery.document_url:
            print(f"Document: {result.status.delivery.document_url}")
    current_health = getattr(result, "health", {}).get("current", {})
    if current_health:
        failed_checks = [check for check in current_health.get("checks", []) if check.get("status") == "fail"]
        issue_word = "issue" if len(failed_checks) == 1 else "issues"
        suffix = f" ({len(failed_checks)} {issue_word})" if failed_checks else ""
        print(f"Health: {current_health.get('overall', 'unknown')}{suffix}")
        signals = current_health.get("signals") or {}
        writer = signals.get("writer") or {}
        if writer.get("llm_requested") and int(writer.get("llm_fallback_count") or 0) > 0:
            print(f"Writer: LLM requested, {int(writer.get('llm_fallback_count') or 0)} rule fallback drafts")
        quality = (signals.get("quality") or {})
        if quality.get("overall") and quality.get("overall") != "full":
            blockers = quality.get("blockers") or []
            missing_count = int(quality.get("missing_count") or 0)
            fallback_count = int(quality.get("fallback_count") or 0)
            print(f"Quality: {quality.get('overall')} ({missing_count} missing, {fallback_count} fallback/partial, {len(blockers)} blockers)")
            for blocker in blockers[:5]:
                name = blocker.get("name") or blocker.get("key") or "unknown"
                status = blocker.get("status") or "unknown"
                print(f"- Quality blocker: {name} {status}")
            if len(blockers) > 5:
                print(f"- Quality blocker: +{len(blockers) - 5} more")
    if result.status.errors:
        print("Errors:")
        for error in result.status.errors:
            print(f"- {error}")
    return 0


@contextlib.contextmanager
def _supervised_scholar_runtime(enabled: bool):
    if not enabled:
        yield
        return
    previous = os.environ.get(SCHOLARLY_RUNTIME_ENV)
    os.environ[SCHOLARLY_RUNTIME_ENV] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(SCHOLARLY_RUNTIME_ENV, None)
        else:
            os.environ[SCHOLARLY_RUNTIME_ENV] = previous


def _feedback_add(args) -> int:
    config = load_config(args.root)
    try:
        if args.text:
            events = record_feedback_text(config, args.text, date_selector=args.date, channel=args.channel, origin="cli", keywords=args.keyword)
        else:
            events = [_feedback_event_from_args(config, args)]
            for event in events:
                append_feedback_event(config, event)
    except ValueError as exc:
        print(f"Feedback error: {exc}")
        print("Example: feedback add --text \"今天第 3 条不行，第 8 条不错\"")
        return 1
    for event in events:
        print(f"Feedback recorded: {event.signal} {event.run_date} #{event.rank} {event.title}")
    return 0


def _feedback_event_from_args(config, args) -> FeedbackEvent:
    if args.rank is None or args.signal is None:
        raise ValueError("Structured feedback requires --rank and --signal, or use --text")
    run_date, item = resolve_published_item(config, args.date or "latest", args.rank)
    return build_feedback_event(item, run_date, args.rank, args.signal, args.channel, args.keyword, args.note, origin="cli")


def _feedback_sync(args) -> int:
    config = load_config(args.root)
    if args.source == "feishu":
        try:
            result = sync_feishu_feedback(config, week=args.week, date_selector=args.date, dry_run=args.dry_run)
        except Exception as exc:
            print(f"Feedback sync error: {exc}")
            return 1
        mode = "dry-run" if args.dry_run else "recorded"
        print(
            f"Feedback sync {mode}: seen={result.seen} parsed={result.parsed} recorded={result.recorded} "
            f"unresolved={result.unresolved} suggestions_applied={result.suggestions_applied} suggestions_rejected={result.suggestions_rejected}"
        )
        for error in result.errors or []:
            print(f"- {error}")
        return 0
    return 1


def _feedback_serve(args) -> int:
    config = load_config(args.root)
    url = feedback_form_action(args.host, args.port)
    print(f"Feedback button server: {url}")
    print("Open the generated HTML report and click 有用 / 不相关. Press Ctrl-C to stop.")
    try:
        serve_feedback(config, args.host, args.port)
    except KeyboardInterrupt:
        print("\nFeedback button server stopped.")
    return 0


def _feedback_set_status(args, status: str) -> int:
    config = load_config(args.root)
    try:
        set_feedback_event_status(config, args.event_id, status, args.reason)
    except ValueError as exc:
        print(f"Feedback status error: {exc}")
        return 1
    print(f"Feedback event {args.event_id} marked {status}")
    return 0


def _feedback_cleanup(args) -> int:
    config = load_config(args.root)
    candidates = [event for event in load_feedback_events(config) if event.status == "active" and _is_likely_test_feedback(event)]
    for event in candidates:
        print(f"candidate {event.event_id} {event.signal} {event.run_date} #{event.rank} {event.title} note={event.note}")
    if args.dry_run:
        print(f"Feedback cleanup dry-run: candidates={len(candidates)}")
        return 0
    for event in candidates:
        set_feedback_event_status(config, event.event_id, "test", "cleanup: likely verification/test feedback")
    print(f"Feedback cleanup marked test: {len(candidates)}")
    return 0


def _feedback_suggest(args) -> int:
    config = load_config(args.root)
    suggestions = generate_feedback_suggestions(config)
    for suggestion in suggestions:
        print(format_suggestion(suggestion))
    if args.dry_run:
        print(f"Feedback suggest dry-run: candidates={len(suggestions)}")
    else:
        save_feedback_suggestions(config, suggestions)
        print(f"Feedback suggestions pending: added={len(suggestions)}")
    return 0


def _feedback_suggestions(args) -> int:
    config = load_config(args.root)
    suggestions = load_feedback_suggestions(config)
    if args.status != "all":
        suggestions = [suggestion for suggestion in suggestions if suggestion.get("status") == args.status]
    for suggestion in suggestions:
        print(format_suggestion(suggestion))
        if suggestion.get("status") == "pending":
            print(f"  confirm: 确认建议 {suggestion.get('id')}")
            print(f"  reject: 拒绝建议 {suggestion.get('id')} 因为 ...")
    print(f"Feedback suggestions: {len(suggestions)}")
    return 0


def _feedback_apply_suggestion(args) -> int:
    config = load_config(args.root)
    try:
        suggestion = apply_feedback_suggestion(config, args.id)
    except ValueError as exc:
        print(f"Feedback suggestion error: {exc}")
        return 1
    print(f"Applied suggestion: {format_suggestion(suggestion)}")
    return 0


def _feedback_reject_suggestion(args) -> int:
    config = load_config(args.root)
    try:
        suggestion = reject_feedback_suggestion(config, args.id, args.reason)
    except ValueError as exc:
        print(f"Feedback suggestion error: {exc}")
        return 1
    print(f"Rejected suggestion: {format_suggestion(suggestion)}")
    return 0


def _feedback_respond(args) -> int:
    config = load_config(args.root)
    result = process_suggestion_response_text(config, args.text, dry_run=args.dry_run)
    if not result.handled:
        print("Feedback suggestion response not recognized. Example: 确认建议 sug_... or 拒绝建议 sug_... 因为 ...")
        return 1
    mode = "dry-run" if args.dry_run else "recorded"
    for response in result.responses:
        print(f"Suggestion response {mode}: {response.action} {response.suggestion_id}{' reason=' + response.reason if response.reason else ''}")
    for error in result.errors:
        print(f"- {error}")
    return 1 if result.errors else 0


def _feedback_profile(args) -> int:
    config = load_config(args.root)
    profile = build_feedback_profile(config, limit=args.limit, include_ignored=args.include_ignored)
    print(render_feedback_profile(profile, config))
    return 0


def _feedback_explain(args) -> int:
    config = load_config(args.root)
    events = load_feedback_events(config)
    if args.rank is not None:
        date_selector = args.date or "latest"
        run_date, item = resolve_published_item(config, date_selector, args.rank)
        events = [event for event in events if event.run_date == run_date and event.key == item["key"]]
    else:
        events = events[-args.limit :]
    library = load_material_library(config)
    feedback_config = config.feedback.get("events", {}) or {}
    print("Feedback explain: approximate impact from events and current material-library snapshots")
    print(
        "Weights: exact like/dislike="
        f"{feedback_config.get('exact_item_like_weight', 8)}/{feedback_config.get('exact_item_dislike_weight', -10)}, "
        f"tag={feedback_config.get('tag_like_weight', 2)}/{feedback_config.get('tag_dislike_weight', -3)}, "
        f"keyword={feedback_config.get('keyword_like_weight', 3)}/{feedback_config.get('keyword_dislike_weight', -4)}, "
        f"caps={feedback_config.get('min_feedback_score', -15)}..{feedback_config.get('max_feedback_score', 12)}"
    )
    grouped = {}
    for event in events:
        grouped.setdefault(event.key, []).append(event)
    for key, item_events in grouped.items():
        snapshot = library.get(key)
        feedback_snapshot = snapshot.score_breakdown.get("feedback") if snapshot else None
        active_like = sum(1 for event in item_events if event.status == "active" and event.signal == "like")
        active_dislike = sum(1 for event in item_events if event.status == "active" and event.signal == "dislike")
        ignored = sum(1 for event in item_events if event.status != "active")
        title = item_events[-1].title
        print(f"- {title} ({key})")
        print(f"  active: like={active_like}, dislike={active_dislike}; ignored={ignored}; feedback_snapshot={feedback_snapshot}")
        for event in item_events:
            print(f"  - {event.event_id} {event.status} {event.signal} #{event.rank} {event.origin}/{event.channel}: {event.note or ''}")
    return 0


def _is_likely_test_feedback(event) -> bool:
    note = (event.note or "").lower()
    if any(marker in note for marker in ["verification", "test", "测试"]):
        return True
    if event.origin == "cli" and event.channel in {"local", "cc-connect"} and note in {"今天第 3 条不行", "第 8 条不错"}:
        return True
    return False


def _feedback_show(args) -> int:
    config = load_config(args.root)
    events = load_feedback_events(config)[-args.limit :]
    if not events:
        print("No feedback events.")
        return 0
    for event in events:
        print(f"{event.created_at} {event.signal} {event.run_date} #{event.rank} {event.title} [{event.status}] {event.event_id} {event.origin}/{event.channel}")
        if event.keywords:
            print(f"  keywords: {', '.join(event.keywords)}")
        if event.note:
            print(f"  note: {event.note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
