from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from plistlib import dumps
from shlex import quote
from typing import Any


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
    launchd_label: str


@dataclass(frozen=True)
class SchedulePreview:
    backend: str
    timezone: str
    jobs: list[ScheduleJob]
    body: str


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


def build_schedule_jobs(config: Any) -> list[ScheduleJob]:
    schedule = config.delivery.get("schedule", {}) or {}
    production_time = _parse_hhmm(schedule.get("production_time", "02:00"), "production_time")
    review_time = _parse_hhmm(schedule.get("review_time", schedule.get("preproduction_time", "07:20")), "review_time")
    target_time = _parse_hhmm(schedule.get("target_time", "08:00"), "target_time")
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
            command=dry_run_command,
            launchd_label="com.daily-agent.overnight-dry-run",
        ),
        ScheduleJob(
            name="morning-review-checkpoint",
            description="Daily_Agent morning source and feedback review checkpoint",
            time=review_time,
            command=review_command,
            launchd_label="com.daily-agent.morning-review-checkpoint",
        ),
        ScheduleJob(
            name="formal-feishu-delivery",
            description="Daily_Agent formal Feishu delivery",
            time=target_time,
            command=feishu_command,
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
            "--timeout-mins 60 "
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
        parts.append(_launchd_plist(job, root).decode("utf-8"))
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
        f"{_quality_check_command(root, python_executable, require_full=True)} && "
        f"{_pipeline_command(root, python_executable, dry_run=True, send=None, require_full=True)}"
    )


def _formal_delivery_command(root: Path, python_executable: str) -> str:
    return (
        f"{_quality_enforce_command(root, python_executable)} && "
        f"{_quality_check_command(root, python_executable, require_full=True)} && "
        f"{_pipeline_command(root, python_executable, dry_run=False, send='feishu', require_full=True)}"
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


def _launchd_plist(job: ScheduleJob, root: Path) -> bytes:
    logs_dir = root / "data" / "logs"
    payload = {
        "Label": job.launchd_label,
        "ProgramArguments": ["/bin/zsh", "-lc", job.command],
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
