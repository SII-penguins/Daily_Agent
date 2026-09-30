from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from plistlib import dumps
from shlex import quote
from typing import Any, Callable


@dataclass(frozen=True)
class ScheduleTime:
    hour: int
    minute: int

    @property
    def cron(self) -> str:
        return f"{self.minute} {self.hour} * * *"


@dataclass(frozen=True)
class ScheduleJob:
    name: str
    description: str
    time: ScheduleTime
    command: str
    launchd_command: str
    launchd_label: str


@dataclass(frozen=True)
class SchedulePreview:
    backend: str
    timezone: str
    jobs: list[ScheduleJob]
    body: str


@dataclass(frozen=True)
class ScheduleStageResult:
    requested_stage: str
    completed_stages: list[str]
    skipped_stages: list[str]
    warnings: list[str]


_SCHEDULE_STAGES = ("overnight", "review", "delivery")
_StageExecutor = Callable[[list[str], Path], int]


def build_schedule_preview(config: Any, backend: str = "cc-connect") -> SchedulePreview:
    jobs = build_schedule_jobs(config)
    if backend == "cc-connect":
        body = render_cc_connect_preview(jobs)
    elif backend == "launchd":
        body = render_launchd_preview(jobs, config.root)
    else:
        raise ValueError(f"Unsupported schedule backend: {backend}")
    timezone = str(config.delivery.get("report", {}).get("timezone", "Asia/Shanghai"))
    return SchedulePreview(backend=backend, timezone=timezone, jobs=jobs, body=body)


def run_scheduled_stage(
    root: str | Path,
    stage: str,
    run_date: date | None = None,
    *,
    executor: _StageExecutor | None = None,
) -> ScheduleStageResult:
    """Prepare with prerequisites; delivery independently sends a sealed issue."""
    if stage not in _SCHEDULE_STAGES:
        raise ValueError(f"Unsupported schedule stage: {stage}")

    root_path = Path(root).resolve()
    target_date = run_date or date.today()
    state_dir = root_path / "data" / "state" / "schedule-stages"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / f"{target_date.isoformat()}.json"
    lock_path = state_dir / ("delivery.lock" if stage == "delivery" else "schedule.lock")
    run_command = executor or _run_stage_command
    required_stages = ("delivery",) if stage == "delivery" else _SCHEDULE_STAGES[: _SCHEDULE_STAGES.index(stage) + 1]

    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        state = _load_stage_state(state_path, target_date)
        if stage == 'review' and executor is None:
            # A stale stage flag is not proof that a sendable issue exists.
            from daily_agent.config import load_config
            cfg = load_config(root_path)
            if not (cfg.state_dir / 'ready-reports' / f'{target_date.isoformat()}.json').exists():
                state['stages'].pop('overnight', None)
        completed: list[str] = []
        skipped: list[str] = []
        warnings: list[str] = []
        for current_stage in required_stages:
            if state["stages"].get(current_stage, {}).get("completed"):
                skipped.append(current_stage)
                continue
            stage_warnings = _execute_stage(current_stage, root_path, target_date, run_command)
            warnings.extend(stage_warnings)
            state["stages"][current_stage] = {
                "completed": True,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "warnings": stage_warnings,
            }
            _write_stage_state(state_path, state)
            completed.append(current_stage)

    return ScheduleStageResult(
        requested_stage=stage,
        completed_stages=completed,
        skipped_stages=skipped,
        warnings=warnings,
    )


def _execute_stage(stage: str, root: Path, run_date: date, executor: _StageExecutor) -> list[str]:
    warnings: list[str] = []
    for command in _stage_commands(stage, root, run_date):
        exit_code = executor(command, root)
        if exit_code == 0:
            continue
        message = f"{stage} command failed with exit code {exit_code}: {' '.join(command)}"
        if stage == "review":
            warnings.append(message)
            continue
        raise RuntimeError(message)
    return warnings


def _stage_commands(stage: str, root: Path, run_date: date) -> list[list[str]]:
    base = [sys.executable, "-m", "daily_agent.cli"]
    root_text = str(root)
    date_text = run_date.isoformat()
    if stage == "overnight":
        return [base + ["run", "--root", root_text, "--date", date_text, "--dry-run", "--llm"]]
    if stage == "review":
        return [
            base + ["feedback", "sync", "--root", root_text, "--source", "feishu", "--week", "latest"],
            base + ["quality", "check", "--root", root_text],
            base + ["source", "check", "--root", root_text, "--date", date_text, "--window-days", "7"],
        ]
    if stage == "delivery":
        return [base + ["deliver-ready", "--root", root_text, "--date", date_text]]
    raise ValueError(f"Unsupported schedule stage: {stage}")


def _run_stage_command(command: list[str], root: Path) -> int:
    try:
        # Bound expensive generation so the morning checkpoint can recover.
        timeout = 18000 if '--dry-run' in command else 480 if 'deliver-ready' in command else 600
        return subprocess.run(command, cwd=root, check=False, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        return 124


def _load_stage_state(path: Path, run_date: date) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict) or payload.get("date") != run_date.isoformat():
        return {"date": run_date.isoformat(), "stages": {}}
    stages = payload.get("stages")
    if not isinstance(stages, dict):
        payload["stages"] = {}
    return payload


def _write_stage_state(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def build_schedule_jobs(config: Any) -> list[ScheduleJob]:
    schedule = config.delivery.get("schedule", {}) or {}
    production_time = _parse_hhmm(schedule.get("production_time", "00:10"), "production_time")
    review_time = _parse_hhmm(schedule.get("review_time", schedule.get("preproduction_time", "05:30")), "review_time")
    target_time = _parse_hhmm(schedule.get("delivery_time", "07:50"), "delivery_time")
    root = Path(config.root)
    python_executable = _python_executable(config)
    dry_run_command = _overnight_dry_run_command(root, python_executable)
    review_command = _review_checkpoint_command(root, python_executable)
    feishu_command = _formal_delivery_command(root, python_executable)
    return [
        ScheduleJob(
            name="overnight-dry-run",
            description="Daily_Agent overnight dry-run",
            time=production_time,
            command=_stage_runner_command(root, python_executable, "overnight"),
            launchd_command=_stage_runner_command(root, python_executable, "overnight"),
            launchd_label="com.daily-agent.overnight-dry-run",
        ),
        ScheduleJob(
            name="morning-review-checkpoint",
            description="Daily_Agent morning source and feedback review checkpoint",
            time=review_time,
            command=_stage_runner_command(root, python_executable, "review"),
            launchd_command=_stage_runner_command(root, python_executable, "review"),
            launchd_label="com.daily-agent.morning-review-checkpoint",
        ),
        ScheduleJob(
            name="formal-feishu-delivery",
            description="Daily_Agent formal cc-connect delivery to Feishu",
            time=target_time,
            command=_stage_runner_command(root, python_executable, "delivery"),
            launchd_command=_stage_runner_command(root, python_executable, "delivery"),
            launchd_label="com.daily-agent.formal-feishu-delivery",
        ),
    ]


def render_cc_connect_preview(jobs: list[ScheduleJob]) -> str:
    lines = [
        "# Preview only: these commands do not run until copied and executed.",
        "# Review generated cron lines before enabling formal delivery.",
    ]
    for job in jobs:
        lines.append(
            "cc-connect cron add "
            f"--cron {quote(job.time.cron)} "
            f"--exec {quote(job.command)} "
            "--session-mode new-per-run "
            f"--timeout-mins {300 if job.name in {'overnight-dry-run', 'formal-feishu-delivery'} else 60} "
            f"--desc {quote(job.description)}"
        )
    return "\n".join(lines)


def render_launchd_preview(jobs: list[ScheduleJob], root: Path) -> str:
    parts = [
        "Preview only: write each plist to ~/Library/LaunchAgents/ and run launchctl bootstrap manually only after review.",
    ]
    for job in jobs:
        filename = f"~/Library/LaunchAgents/{job.launchd_label}.plist"
        parts.append(f"\n# {filename}")
        parts.append(_launchd_plist(job, root, job.launchd_command).decode("utf-8"))
    return "\n".join(parts)


def _pipeline_command(root: Path, python_executable: str, dry_run: bool, send: str | None, require_full: bool = False) -> str:
    parts = [
        f"PYTHONPATH={quote(str(root / 'src'))}",
        quote(python_executable),
        "-m",
        "daily_agent.cli",
        "run",
        "--root",
        quote(str(root)),
        "--date",
        "today",
        "--llm",
    ]
    if dry_run:
        parts.append("--dry-run")
    if send:
        parts.extend(["--send", quote(send)])
    if require_full:
        parts.append("--require-full")
    return " ".join(parts)


def _review_checkpoint_command(root: Path, python_executable: str) -> str:
    return (
        f"{_quality_enforce_command(root, python_executable)} && "
        f"{_quality_check_command(root, python_executable)} && "
        f"{_source_check_command(root, python_executable)}"
    )


def _overnight_dry_run_command(root: Path, python_executable: str) -> str:
    return (
        f"{_quality_enforce_command(root, python_executable)} && "
        f"{_quality_check_command(root, python_executable)} && "
        f"{_pipeline_command(root, python_executable, dry_run=True, send=None)}"
    )


def _formal_delivery_command(root: Path, python_executable: str) -> str:
    return (
        f"{_quality_enforce_command(root, python_executable)} && "
        f"{_quality_check_command(root, python_executable)} && "
        f"{_pipeline_command(root, python_executable, dry_run=False, send='cc-connect')} --allow-degraded"
    )


def _stage_runner_command(root: Path, python_executable: str, stage: str) -> str:
    return " ".join(
        [
            f"PYTHONPATH={quote(str(root / 'src'))}",
            quote(python_executable),
            "-m",
            "daily_agent.cli",
            "schedule",
            "run-stage",
            "--root",
            quote(str(root)),
            "--stage",
            stage,
        ]
    )


def _quality_enforce_command(root: Path, python_executable: str) -> str:
    parts = [
        f"PYTHONPATH={quote(str(root / 'src'))}",
        quote(python_executable),
        "-m",
        "daily_agent.cli",
        "quality",
        "enforce-full",
        "--root",
        quote(str(root)),
        "--write",
    ]
    return " ".join(parts)


def _quality_check_command(root: Path, python_executable: str, require_full: bool = False) -> str:
    parts = [
        f"PYTHONPATH={quote(str(root / 'src'))}",
        quote(python_executable),
        "-m",
        "daily_agent.cli",
        "quality",
        "check",
        "--root",
        quote(str(root)),
    ]
    if require_full:
        parts.append("--require-full")
    return " ".join(parts)


def _source_check_command(root: Path, python_executable: str) -> str:
    parts = [
        f"PYTHONPATH={quote(str(root / 'src'))}",
        quote(python_executable),
        "-m",
        "daily_agent.cli",
        "source",
        "check",
        "--root",
        quote(str(root)),
        "--date",
        "today",
        "--window-days",
        "7",
    ]
    return " ".join(parts)


def _python_executable(config: Any) -> str:
    runtime = config.delivery.get("runtime", {}) or {}
    configured = runtime.get("python_executable")
    if configured:
        return str(configured)
    return sys.executable


def _launchd_plist(job: ScheduleJob, root: Path, command: str) -> bytes:
    logs_dir = root / "data" / "logs"
    payload = {
        "Label": job.launchd_label,
        "ProgramArguments": ["/bin/zsh", "-lc", command],
        "StartCalendarInterval": {"Hour": job.time.hour, "Minute": job.time.minute},
        "StandardOutPath": str(logs_dir / f"{job.name}.out.log"),
        "StandardErrorPath": str(logs_dir / f"{job.name}.err.log"),
        "WorkingDirectory": str(root),
    }
    return dumps(payload, sort_keys=False)


def _parse_hhmm(value: Any, field_name: str) -> ScheduleTime:
    text = str(value)
    pieces = text.split(":")
    if len(pieces) != 2:
        raise ValueError(f"schedule.{field_name} must use HH:MM")
    try:
        hour = int(pieces[0])
        minute = int(pieces[1])
    except ValueError as exc:
        raise ValueError(f"schedule.{field_name} must use HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"schedule.{field_name} must use HH:MM")
    return ScheduleTime(hour=hour, minute=minute)


def seal_ready_report(config, run_date):
    """Seal a completed daily issue; intermediate editorial snapshots are not sendable."""
    import hashlib
    from daily_agent.paper_document import atomic_json
    day = run_date.isoformat()
    report = config.reports_dir / f"daily-agent-{day}.md"
    approval = config.root / 'data' / 'editorial' / day / 'approval.json'
    if not report.exists() or not approval.exists():
        return
    rows = json.loads(approval.read_text())
    if not rows:
        return
    ready = config.reports_dir / f"daily-agent-{day}.ready.md"
    temporary = ready.with_suffix('.tmp')
    temporary.write_bytes(report.read_bytes())
    os.replace(temporary, ready)
    atomic_json(config.state_dir / 'ready-reports' / f'{day}.json', {
        'date': day, 'report': str(ready.resolve()),
        'sha256': hashlib.sha256(ready.read_bytes()).hexdigest(), 'approval': rows})


def deliver_ready_report(config, run_date):
    """Send the sealed current-day issue only; never invoke collection or an LLM."""
    import hashlib
    from daily_agent.models import ApprovedItem, MaterialRecord
    from daily_agent.delivery.feishu import deliver_weekly_report
    from daily_agent.storage import mark_materials_published, write_published_index, write_selected
    from daily_agent.paper_document import atomic_json
    path = config.state_dir / 'ready-reports' / f'{run_date.isoformat()}.json'
    payload = json.loads(path.read_text())
    if payload.get('date') != run_date.isoformat() or not payload.get('approval'):
        raise RuntimeError('No completed report for today')
    report = Path(payload['report'])
    if hashlib.sha256(report.read_bytes()).hexdigest() != payload.get('sha256'):
        raise RuntimeError('Ready report identity mismatch')
    receipt = path.with_suffix('.delivered.json')
    if receipt.exists() and json.loads(receipt.read_text()).get('sha256') == payload['sha256']:
        return
    items = [ApprovedItem(**{**row, 'material': MaterialRecord.from_dict(row['material'])}) for row in payload['approval']]
    status = deliver_weekly_report(report, 'cc-connect', config=config, run_date=run_date,
                                  daily_markdown=report.read_text())
    if not status.ok:
        raise RuntimeError(status.error or 'Delivery failed')
    atomic_json(receipt, {'date': run_date.isoformat(), 'sha256': payload['sha256'],
                         'delivered_at': datetime.now(timezone.utc).isoformat(), 'status': status.to_dict()})
    mark_materials_published(config, [item.material for item in items], run_date)
    write_published_index(config, items, run_date)
    write_selected(config, items, run_date)
