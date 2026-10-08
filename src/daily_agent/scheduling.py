from __future__ import annotations

import json
import hashlib
import math
import random
import shutil
import uuid
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from plistlib import dumps
from shlex import quote
from typing import Any, Callable
from zoneinfo import ZoneInfo

from daily_agent.workflow_state import StateCorrupt, atomic_bytes, atomic_json, exclusive_lock, read_json
from daily_agent.workflow_runtime import (CleanupPending, WorkflowCancelled, inspect_orphan,
                                         run_process, stop_verified_orphan, validate_process_identity)


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
    timeout_minutes: int = 300


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


class StageFailure(RuntimeError):
    def __init__(self, message: str, *, kind: str = "permanent", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


def _config(root: Path):
    from daily_agent.config import load_config
    return load_config(root)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _policy(root: Path) -> dict[str, float]:
    values = (_config(root).delivery.get("schedule", {}).get("recovery", {}) or {})
    defaults = {"max_attempts": 3, "base_backoff_seconds": 30, "max_backoff_seconds": 300,
                "heartbeat_seconds": 30, "stale_seconds": 180, "generation_timeout_seconds": 18000,
                "check_timeout_seconds": 600, "delivery_timeout_seconds": 480,
                "overnight_budget_seconds": 18000, "review_budget_seconds": 7800,
                "delivery_budget_seconds": 600, "deadline_buffer_seconds": 120}
    policy = {key: float(values.get(key, value)) for key, value in defaults.items()}
    if (any(not math.isfinite(value) or value < 0 for value in policy.values())
            or policy["max_attempts"] < 1 or policy["max_attempts"] > 10
            or not policy["max_attempts"].is_integer()
            or policy["heartbeat_seconds"] <= 0 or policy["stale_seconds"] < 2 * policy["heartbeat_seconds"]):
        raise ValueError("Invalid schedule.recovery policy")
    return policy


def _revision(root: Path) -> str:
    """Configuration/code changes permit a new bounded recovery cycle. No secrets included."""
    hasher = hashlib.sha256()
    for path in sorted([*(root / "config").glob("*.yaml"), *(root / "src/daily_agent").rglob("*.py")]):
        hasher.update(str(path.relative_to(root)).encode())
        hasher.update(path.read_bytes())
    # Adding missing credentials/tools is also a meaningful repair. Store only
    # the fingerprint of capability booleans, never secret values or their hashes.
    from daily_agent.secrets import FULL_PROFILE_SECRET_GROUPS, credential_present
    capabilities = {name: any(credential_present(key) for key in names)
                    for name, names in FULL_PROFILE_SECRET_GROUPS}
    capabilities.update(python=sys.version, codex=shutil.which("codex"), cc_connect=shutil.which("cc-connect"))
    hasher.update(json.dumps(capabilities, sort_keys=True).encode())
    return hasher.hexdigest()


def _state_path(root: Path, run_date: date) -> Path:
    return _config(root).state_dir / "schedule-stages" / f"{run_date.isoformat()}.json"


def _stage_lock(root: Path) -> Path:
    return _config(root).state_dir / "schedule-stages" / "schedule.lock"


def _event(state: dict, stage: str, kind: str, **details) -> None:
    state.setdefault("events", []).append({"at": _now().isoformat(), "stage": stage, "kind": kind, **details})
    state["events"] = state["events"][-100:]


def _validate_stage_state(payload, run_date: date) -> dict:
    if (not isinstance(payload, dict) or payload.get("date") != run_date.isoformat()
            or not isinstance(payload.get("stages"), dict)
            or not all(isinstance(value, dict) for value in payload["stages"].values())
            or not isinstance(payload.get("events", []), list)):
        raise StateCorrupt("Invalid daily stage state")
    for entry in payload["stages"].values():
        if (type(entry.get("attempts", 0)) is not int or entry.get("attempts", 0) < 0
                or not isinstance(entry.get("commands", {}), dict)
                or not isinstance(entry.get("budget_windows", {}), dict)
                or any(type(entry[key]) is not bool for key in
                       ("running", "waiting", "completed", "failed", "circuit_open", "cleanup_pending") if key in entry)
                or not all(isinstance(value, dict) for value in entry.get("commands", {}).values())):
            raise StateCorrupt("Invalid stage attempts/checkpoints")
        if entry.get("child_identity") is not None:
            try:
                validate_process_identity(entry["child_identity"])
            except ValueError as exc:
                raise StateCorrupt("Invalid stage child identity") from exc
        previous = entry.get("cleanup_previous_failure")
        if previous is not None and (not isinstance(previous, dict)
                or any(type(previous.get(key)) is not bool for key in ("failed", "circuit_open"))
                or not isinstance(previous.get("failure_kind"), str)
                or not isinstance(previous.get("error"), str)):
            raise StateCorrupt("Invalid failure retained during cleanup")
        try:
            clocks = list(entry.get("budget_windows", {}).values())
            if "next_retry_at" in entry:
                clocks.append(entry["next_retry_at"])
            if any(datetime.fromisoformat(value).tzinfo is None for value in clocks):
                raise ValueError("Naive recovery clock")
        except (ValueError, TypeError) as exc:
            raise StateCorrupt("Invalid stage recovery clock") from exc
    return payload


def _load_stage_state(path: Path, run_date: date) -> dict[str, Any]:
    payload = read_json(path)
    if payload is None:
        return {"schema_version": 2, "date": run_date.isoformat(), "stages": {}, "events": []}
    return _validate_stage_state(payload, run_date)


def _write_stage_state(path: Path, payload: dict[str, Any]) -> None:
    # Keep the previous valid checkpoint for explicit, audited repair only.
    _validate_stage_state(payload, date.fromisoformat(payload["date"]))
    if path.exists():
        previous = read_json(path)
        _validate_stage_state(previous, date.fromisoformat(payload["date"]))
        atomic_bytes(path.with_suffix(".bak"), path.read_bytes())
    atomic_json(path, payload)


def _record_cleanup_pending(state, path, stage, entry, error):
    # A cleanup blocker must not silently forgive an earlier permanent failure.
    if not entry.get("cleanup_pending") and (entry.get("failed") or entry.get("circuit_open")):
        entry["cleanup_previous_failure"] = {
            "failed": bool(entry.get("failed")), "circuit_open": bool(entry.get("circuit_open")),
            "failure_kind": str(entry.get("failure_kind") or "failed"), "error": str(entry.get("error") or ""),
        }
    entry.update(running=False, waiting=False, completed=False, failed=True, circuit_open=True,
                 cleanup_pending=True, failure_kind="cleanup_pending", error=str(error),
                 failed_at=_now().isoformat())
    _event(state, stage, "cleanup_pending", error=str(error))
    _write_stage_state(path, state)


def _reclaim_interrupted_attempt(state, path, stage, entry, root):
    if not (entry.get("running") or entry.get("cleanup_pending") or entry.get("child_identity") is not None):
        return
    try:
        if entry.get("cleanup_pending") and entry.get("child_identity") is None:
            raise CleanupPending("Missing cleanup identity; operator review required")
        stopped = stop_verified_orphan(entry.get("child_identity"), root)
    except (Exception, KeyboardInterrupt) as exc:
        _record_cleanup_pending(state, path, stage, entry, exc)
        raise StageFailure(str(exc), kind="cleanup_pending") from exc
    entry.update(running=False, child_identity=None, cleanup_pending=False)
    if entry.get("failure_kind") == "cleanup_pending":
        previous = entry.pop("cleanup_previous_failure", None)
        if previous is not None:
            entry.update(previous)
        else:
            entry.update(failed=False, circuit_open=False)
            for key in ("failure_kind", "error", "failed_at"):
                entry.pop(key, None)
    _event(state, stage, "reclaim_interrupted_attempt", orphan_stopped=stopped)
    _write_stage_state(path, state)


def _calendar_deadline(root: Path, run_date: date, stage: str) -> datetime:
    config = _config(root)
    schedule = config.delivery.get("schedule", {})
    field = {"overnight": "review_time", "review": "delivery_time", "delivery": "target_time"}[stage]
    fallback = {"overnight": "05:30", "review": "07:50", "delivery": "08:00"}[stage]
    parsed = _parse_hhmm(schedule.get(field, fallback), field)
    zone = ZoneInfo(config.delivery.get("report", {}).get("timezone", "Asia/Shanghai"))
    deadline = datetime.combine(run_date, datetime_time(parsed.hour, parsed.minute), zone)
    if stage != "delivery":
        deadline -= timedelta(seconds=_policy(root)["deadline_buffer_seconds"])
    return deadline.astimezone(timezone.utc)


def _stage_is_completed(state: dict[str, Any], stage: str, root: Path, run_date: date,
                        *, verify_artifacts: bool = True) -> bool:
    entry = state.get("stages", {}).get(stage, {})
    if (not entry.get("completed") or entry.get("failed") or entry.get("running")
            or entry.get("child_identity") or entry.get("cleanup_pending")):
        return False
    if not verify_artifacts:
        return True
    try:
        payload = validate_ready_report(_config(root), run_date)
        if stage == "delivery":
            receipt = _delivery_receipt(_config(root), run_date, payload)
            return bool(receipt and receipt.get("publication_reconciled"))
        return True
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return False


def run_scheduled_stage(root: str | Path, stage: str, run_date: date | None = None,
                        *, executor: _StageExecutor | None = None) -> ScheduleStageResult:
    """One bounded controller, durable command checkpoints, and an independent sealed delivery."""
    if stage not in _SCHEDULE_STAGES:
        raise ValueError(f"Unsupported schedule stage: {stage}")
    root = Path(root).resolve()
    config = _config(root)
    day = run_date or _now().astimezone(ZoneInfo(config.delivery.get("report", {}).get("timezone", "Asia/Shanghai"))).date()
    path = _state_path(root, day)
    policy = _policy(root)
    required = ("delivery",) if stage == "delivery" else _SCHEDULE_STAGES[:_SCHEDULE_STAGES.index(stage) + 1]
    completed, skipped, warnings = [], [], []
    revision = _revision(root)
    with exclusive_lock(_stage_lock(root)):
        state = _load_stage_state(path, day)
        # The same-day lease covers all stages. Retained work from another stage
        # cannot be bypassed by selecting delivery or a different revision.
        for name, previous in state["stages"].items():
            _reclaim_interrupted_attempt(state, path, name, previous, root)
        for current in required:
            if _stage_is_completed(state, current, root, day, verify_artifacts=executor is None):
                skipped.append(current)
                continue
            entry = state["stages"].setdefault(current, {})
            if entry.get("completed") and current == "overnight":
                # Its output was deleted/corrupted; a completed command checkpoint
                # is no longer enough to resume generation.
                entry["commands"] = {}
                _event(state, current, "invalidate_missing_output_checkpoint")
            if executor is None and current == "overnight":
                try:
                    validate_ready_report(config, day)
                except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                    pass
                else:
                    entry.update(completed=True, running=False, waiting=False, failed=False, completed_at=_now().isoformat())
                    entry.pop("next_retry_at", None)
                    _event(state, current, "adopt_valid_sealed_report")
                    _write_stage_state(path, state)
                    skipped.append(current)
                    continue
            if executor is None and current == "delivery":
                try:
                    payload = validate_ready_report(config, day)
                    receipt = _delivery_receipt(config, day, payload)
                except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                    receipt = None
                if receipt:
                    _reconcile_publication(config, day, payload, receipt)
                    entry.update(completed=True, running=False, waiting=False, failed=False, circuit_open=False,
                                 completed_at=_now().isoformat())
                    entry.pop("next_retry_at", None)
                    _event(state, current, "adopt_confirmed_delivery")
                    _write_stage_state(path, state)
                    skipped.append(current)
                    continue
            if entry.get("revision") != revision:
                if entry:
                    _event(state, current, "revision_changed", previous_attempts=entry.get("attempts", 0))
                entry.update(attempts=0, commands={}, failed=False, circuit_open=False, waiting=False,
                             revision=revision, budget_windows={})
                entry.pop("next_retry_at", None)
            window_stage = "review" if stage == "review" and current == "overnight" else current
            if (window_stage == "review" and current == "overnight" and entry.get("failure_kind") == "deadline"
                    and entry.get("attempts", 0) < int(policy["max_attempts"])):
                # The morning recovery window is distinct from the overnight
                # production window, but does not grant a new retry count.
                entry.update(circuit_open=False, failed=False)
                _event(state, current, "enter_morning_recovery_window")
            if entry.get("circuit_open") or entry.get("attempts", 0) >= int(policy["max_attempts"]):
                raise StageFailure(f"{current} circuit is open; repair the cause before retrying", kind="circuit_open")
            budget = min(policy[f"{current}_budget_seconds"], policy[f"{window_stage}_budget_seconds"])
            if executor is None:
                now = _now()
                expires = min(_calendar_deadline(root, day, window_stage), now + timedelta(seconds=budget))
                windows = entry.setdefault("budget_windows", {})
                if window_stage in windows:
                    expires = min(expires, datetime.fromisoformat(windows[window_stage]))
                windows[window_stage] = expires.isoformat()
                budget = (expires - now).total_seconds()
            deadline = time.monotonic() + max(0, budget)
            while True:
                try:
                    _wait_for_retry(state, path, entry, current, deadline, policy)
                    entry.update(running=True, waiting=False, completed=False, failed=False, owner_pid=os.getpid(),
                                 attempt_id=uuid.uuid4().hex, started_at=_now().isoformat(), heartbeat_at=_now().isoformat())
                    if time.monotonic() >= deadline:
                        raise StageFailure(f"{current} execution window expired", kind="deadline")
                    entry["attempts"] = int(entry.get("attempts", 0)) + 1
                    _event(state, current, "attempt_started", attempt=entry["attempts"])
                    _write_stage_state(path, state)
                    stage_warnings = _execute_checkpointed_stage(current, root, day, state, path, entry,
                                                                 deadline, policy, executor)
                    if executor is None:
                        try:
                            payload = validate_ready_report(config, day)
                        except FileNotFoundError as exc:
                            raise StageFailure("Generation exited without a sealed report", kind="no_output") from exc
                        if current == "delivery" and not _delivery_receipt(config, day, payload):
                            raise StageFailure("Delivery exited without a confirmed receipt", kind="delivery_uncertain")
                    warnings.extend(stage_warnings)
                    entry.update(running=False, waiting=False, completed=True, failed=False, circuit_open=False,
                                 completed_at=_now().isoformat(), warnings=stage_warnings, child_identity=None,
                                 cleanup_pending=False)
                    _event(state, current, "completed")
                    _write_stage_state(path, state)
                    completed.append(current)
                    break
                except (Exception, KeyboardInterrupt) as exc:
                    if isinstance(exc, CleanupPending):
                        _record_cleanup_pending(state, path, current, entry, exc)
                        raise StageFailure(str(exc), kind="cleanup_pending") from exc
                    if isinstance(exc, StageFailure):
                        failure = exc
                    elif isinstance(exc, (WorkflowCancelled, KeyboardInterrupt)):
                        failure = StageFailure("Workflow cancelled", kind="cancelled")
                    else:
                        failure = StageFailure(f"{current}: {type(exc).__name__}: {exc}")
                    retryable = failure.retryable and entry.get("attempts", 0) < int(policy["max_attempts"])
                    entry.update(running=False, waiting=False, completed=False, failed=True, failed_at=_now().isoformat(),
                                 error=str(failure), failure_kind=failure.kind, circuit_open=not retryable,
                                 child_identity=None, cleanup_pending=False)
                    _event(state, current, "failed", failure_kind=failure.kind)
                    _write_stage_state(path, state)
                    if not retryable:
                        raise failure from exc
                    delay = min(policy["max_backoff_seconds"], policy["base_backoff_seconds"] * 2 ** (entry["attempts"] - 1))
                    delay *= random.uniform(.8, 1.2)
                    if time.monotonic() + delay >= deadline:
                        entry.update(circuit_open=True, failure_kind="deadline")
                        _write_stage_state(path, state)
                        raise StageFailure(f"{current} recovery window expired", kind="deadline") from exc
                    entry["next_retry_at"] = (_now() + timedelta(seconds=delay)).isoformat()
                    entry.update(failed=False, waiting=True)
                    _event(state, current, "retry_scheduled", delay_seconds=round(delay, 3))
                    _write_stage_state(path, state)
    return ScheduleStageResult(stage, completed, skipped, warnings)


def _wait_for_retry(state, path, entry, stage, deadline, policy):
    """The persisted ETA survives a controller restart; waiting consumes time, not attempts."""
    if "next_retry_at" not in entry:
        return
    eta = datetime.fromisoformat(entry["next_retry_at"])
    while True:
        remaining = (eta - _now()).total_seconds()
        if remaining <= 0:
            entry.pop("next_retry_at", None)
            entry["waiting"] = False
            return
        budget = deadline - time.monotonic()
        if remaining >= budget:
            raise StageFailure(f"{stage} retry cannot fit its remaining execution window", kind="deadline")
        entry.update(waiting=True, running=False, failed=False, owner_pid=os.getpid(), heartbeat_at=_now().isoformat())
        _write_stage_state(path, state)
        time.sleep(min(remaining, policy["heartbeat_seconds"], 60))


def _execute_checkpointed_stage(stage, root, run_date, state, path, entry, deadline, policy, executor):
    warnings = []
    for index, command in enumerate(_stage_commands(stage, root, run_date)):
        key = hashlib.sha256(json.dumps(command).encode()).hexdigest()
        checkpoint = entry.setdefault("commands", {}).get(key, {})
        if checkpoint.get("status") in {"completed", "advisory_failed"}:
            if checkpoint.get("warning"):
                warnings.append(checkpoint["warning"])
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StageFailure("Stage time budget exhausted", kind="deadline")
        if executor:
            code = executor(command, root)
        else:
            timeout_key = "generation_timeout_seconds" if "--dry-run" in command else "delivery_timeout_seconds" if "deliver-ready" in command else "check_timeout_seconds"
            prefix = _config(root).logs_dir / "schedule-attempts" / f"{run_date}-{stage}-{entry['attempts']}-{index}"
            entry["log_prefix"] = str(prefix)
            def heartbeat():
                entry["heartbeat_at"] = _now().isoformat()
                _write_stage_state(path, state)
            def started(identity):
                entry["child_identity"] = identity
                heartbeat()
            code = _run_stage_command(command, root, timeout=min(policy[timeout_key], remaining),
                                      on_tick=heartbeat, on_start=started, log_prefix=prefix,
                                      heartbeat_seconds=policy["heartbeat_seconds"])
        entry["child_identity"] = None
        message = f"{stage} command failed with exit code {code}: {' '.join(command)}"
        if code == 0:
            entry["commands"][key] = {"status": "completed", "completed_at": _now().isoformat()}
        elif stage == "review" and "feedback" in command:
            warnings.append(message)
            entry["commands"][key] = {"status": "advisory_failed", "warning": message}
        else:
            kind = {75: "transient", 124: "timeout", 2: "preflight", 3: "delivery_uncertain", 4: "no_output"}.get(code, "permanent")
            raise StageFailure(message, kind=kind, retryable=code in {75, 124})
        _write_stage_state(path, state)
    return warnings


def _stage_commands(stage: str, root: Path, run_date: date) -> list[list[str]]:
    base = [sys.executable, "-m", "daily_agent.cli"]
    scope = ["--root", str(root)]
    day = ["--date", run_date.isoformat()]
    if stage == "overnight":
        return [base + ["run"] + scope + day + ["--dry-run", "--llm"]]
    if stage == "review":
        return [base + ["feedback", "sync"] + scope + ["--source", "feishu", "--week", "latest"],
                base + ["quality", "check"] + scope,
                base + ["source", "check"] + scope + day + ["--window-days", "7", "--require-available"]]
    if stage == "delivery":
        return [base + ["deliver-ready"] + scope + day]
    raise ValueError(f"Unsupported schedule stage: {stage}")


def _run_stage_command(command, root, *, timeout=None, **options) -> int:
    if timeout is None:
        timeout = 18000 if "--dry-run" in command else 480 if "deliver-ready" in command else 600
    return run_process(command, root, timeout, **options)


def inspect_schedule_state(root: str | Path, run_date: date) -> dict[str, Any]:
    """Read-only readiness/liveness report, aware of configured due times and timezone."""
    root = Path(root).resolve()
    config = _config(root)
    path = _state_path(root, run_date)
    problems = []
    try:
        state = _load_stage_state(path, run_date)
    except StateCorrupt as exc:
        state = {"date": run_date.isoformat(), "stages": {}}
        problems.append({"stage": "controller", "kind": "corrupt_state", "error": str(exc)})
    now = _now()
    schedule = config.delivery.get("schedule", {})
    zone = ZoneInfo(config.delivery.get("report", {}).get("timezone", "Asia/Shanghai"))
    for stage, field, fallback in (("overnight", "production_time", "00:10"), ("review", "review_time", "05:30"), ("delivery", "delivery_time", "07:50")):
        entry = state["stages"].get(stage, {})
        cleanup_reason = None
        if entry.get("child_identity") is not None:
            try:
                group = inspect_orphan(entry["child_identity"], root)
                if entry.get("cleanup_pending") or not entry.get("running"):
                    cleanup_reason = ("Process group is empty; run schedule repair to release the checkpoint"
                                      if group == "empty" else "Verified live process group; locked cleanup required")
            except CleanupPending as exc:
                cleanup_reason = str(exc)
        elif entry.get("cleanup_pending"):
            cleanup_reason = "Missing cleanup identity; operator review required"
        if cleanup_reason:
            problems.append({"stage": stage, "kind": "cleanup_pending", "error": cleanup_reason})
            continue
        parsed = _parse_hhmm(schedule.get(field, fallback), field)
        due = datetime.combine(run_date, datetime_time(parsed.hour, parsed.minute), zone)
        if not entry:
            if now >= due:
                problems.append({"stage": stage, "kind": "not_started"})
        elif entry.get("running") or entry.get("waiting"):
            try:
                stamp = datetime.fromisoformat(entry.get("heartbeat_at") or entry.get("started_at"))
                age = (now - stamp).total_seconds()
                if age < -60 or age > _policy(root)["stale_seconds"]:
                    problems.append({"stage": stage, "kind": "stale", "age_seconds": int(age)})
            except (TypeError, ValueError):
                problems.append({"stage": stage, "kind": "stale", "reason": "invalid heartbeat"})
        elif entry.get("circuit_open") or entry.get("failed"):
            problems.append({"stage": stage, "kind": entry.get("failure_kind", "failed"), "error": entry.get("error")})
        elif entry.get("completed") and not _stage_is_completed(state, stage, root, run_date):
            problems.append({"stage": stage, "kind": "missing_or_invalid_artifact"})
    readiness = {"ready": False, "delivered": False}
    try:
        payload = validate_ready_report(config, run_date)
        readiness["ready"] = True
        readiness["sha256"] = payload["sha256"]
        receipt = _delivery_receipt(config, run_date, payload)
        readiness["delivered"] = bool(receipt)
        readiness["publication_reconciled"] = bool(receipt and receipt.get("publication_reconciled"))
        if receipt and not readiness["publication_reconciled"]:
            problems.append({"stage": "delivery", "kind": "local_publication_pending"})
        outbox = _read_outbox(config, run_date, payload)
        if outbox and outbox.get("status") in {"sending", "uncertain"} and not readiness["delivered"]:
            problems.append({"stage": "delivery", "kind": "delivery_uncertain"})
    except FileNotFoundError:
        pass
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        problems.append({"stage": "delivery", "kind": "invalid_artifact", "error": str(exc)})
    return {"date": run_date.isoformat(), "state_path": str(path), "state": state,
            "readiness": readiness, "problems": problems, "ok": not problems}


def repair_schedule_state(root: str | Path, run_date: date, stage: str | None = None,
                          *, reset_budget: bool = False) -> dict[str, Any]:
    """Locked local reconciliation; never clears ambiguous external delivery evidence."""
    root = Path(root).resolve()
    path = _state_path(root, run_date)
    repaired, blocked = [], []
    with exclusive_lock(_stage_lock(root)):
        try:
            state = _load_stage_state(path, run_date)
        except StateCorrupt:
            # Restore a verified backup; absence of one needs operator investigation.
            backup = path.with_suffix(".bak")
            state = _validate_stage_state(read_json(backup), run_date)
            quarantine = path.with_name(path.name + ".corrupt." + uuid.uuid4().hex)
            os.replace(path, quarantine)
            _event(state, "controller", "restore_backup", quarantine=str(quarantine))
        revision = _revision(root)
        for name in [stage] if stage else _SCHEDULE_STAGES:
            if name not in _SCHEDULE_STAGES:
                raise ValueError("Unsupported schedule stage")
            entry = state["stages"].get(name, {})
            try:
                _reclaim_interrupted_attempt(state, path, name, entry, root)
            except StageFailure as exc:
                blocked.append({"stage": name, "reason": f"cleanup_pending; {exc}"})
                continue
            if name == "delivery":
                config = _config(root)
                try:
                    payload = validate_ready_report(config, run_date)
                    outbox = _read_outbox(config, run_date, payload)
                    if outbox and outbox.get("status") in {"sending", "uncertain"} and not _delivery_receipt(config, run_date, payload):
                        blocked.append({"stage": name, "reason": "delivery_uncertain; confirm remote outcome first"})
                        continue
                except FileNotFoundError:
                    pass
            if not entry or _stage_is_completed(state, name, root, run_date):
                continue
            if entry.get("revision") != revision or reset_budget:
                entry.update(attempts=0, commands={}, circuit_open=False, failed=False, completed=False,
                             waiting=False, budget_windows={},
                             revision=revision, recovered_at=_now().isoformat(),
                             recovery_count=int(entry.get("recovery_count", 0)) + 1)
                entry.pop("next_retry_at", None)
                _event(state, name, "reset_after_repair", explicit_reset=reset_budget)
                repaired.append(name)
            elif not entry.get("circuit_open"):
                entry.update(running=False, completed=False)
                repaired.append(name)
            else:
                blocked.append({"stage": name, "reason": "cause unchanged; fix it or explicitly reset budget"})
        _write_stage_state(path, state)
    return {"date": run_date.isoformat(), "repaired": repaired, "blocked": blocked, "state_path": str(path)}


def build_schedule_jobs(config: Any) -> list[ScheduleJob]:
    schedule = config.delivery.get("schedule", {}) or {}
    recovery = schedule.get("recovery", {}) or {}
    production_time = _parse_hhmm(schedule.get("production_time", "00:10"), "production_time")
    review_time = _parse_hhmm(schedule.get("review_time", schedule.get("preproduction_time", "05:30")), "review_time")
    target_time = _parse_hhmm(schedule.get("delivery_time", "07:50"), "delivery_time")
    root = Path(config.root).resolve()
    python_executable = _python_executable(config)
    return [
        ScheduleJob(
            name="overnight-dry-run",
            description="Daily_Agent overnight dry-run",
            time=production_time,
            command=_stage_runner_command(root, python_executable, "overnight"),
            launchd_command=_stage_runner_command(root, python_executable, "overnight"),
            launchd_label="com.daily-agent.overnight-dry-run",
            timeout_minutes=math.ceil(float(recovery.get("overnight_budget_seconds", 18000)) / 60) + 2,
        ),
        ScheduleJob(
            name="morning-review-checkpoint",
            description="Daily_Agent morning source and feedback review checkpoint",
            time=review_time,
            command=_stage_runner_command(root, python_executable, "review"),
            launchd_command=_stage_runner_command(root, python_executable, "review"),
            launchd_label="com.daily-agent.morning-review-checkpoint",
            timeout_minutes=math.ceil(float(recovery.get("review_budget_seconds", 7800)) / 60) + 2,
        ),
        ScheduleJob(
            name="formal-feishu-delivery",
            description="Daily_Agent formal cc-connect delivery to Feishu",
            time=target_time,
            command=_stage_runner_command(root, python_executable, "delivery"),
            launchd_command=_stage_runner_command(root, python_executable, "delivery"),
            launchd_label="com.daily-agent.formal-feishu-delivery",
            timeout_minutes=math.ceil(float(recovery.get("delivery_budget_seconds", 600)) / 60) + 2,
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
            f"--timeout-mins {job.timeout_minutes} "
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


def _ready_path(config, run_date: date) -> Path:
    return config.state_dir / "ready-reports" / f"{run_date.isoformat()}.json"


def _approval_hash(rows) -> str:
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _approved_items(rows):
    from daily_agent.models import ApprovedItem, MaterialRecord
    if not isinstance(rows, list) or not rows:
        raise StageFailure("No approved report items", kind="no_output")
    try:
        items = [ApprovedItem(**{**row, "material": MaterialRecord.from_dict(row["material"])}) for row in rows]
        if (len({item.key for item in items}) != len(items)
                or any(not isinstance(item.key, str) or not item.key or item.key != item.material.key
                       or item.item_type not in {"paper", "repo"} or item.item_type != item.material.item_type
                       or item.title != item.material.title or not isinstance(item.final_fields, dict) for item in items)):
            raise ValueError("Invalid approval identities")
    except (KeyError, TypeError, ValueError) as exc:
        raise StageFailure("Malformed approval data", kind="invalid_artifact") from exc
    return items


def validate_ready_report(config, run_date: date) -> dict:
    """Readiness is validated content and approval identity, never mere file existence."""
    path = _ready_path(config, run_date)
    payload = read_json(path)
    if payload is None:
        raise FileNotFoundError(f"No sealed report for {run_date.isoformat()}")
    if (not isinstance(payload, dict) or payload.get("date") != run_date.isoformat()
            or payload.get("schema_version", 1) not in {1, 2, 3}):
        raise StageFailure("Invalid ready-report manifest", kind="invalid_artifact")
    items = _approved_items(payload.get("approval"))
    try:
        report = Path(payload["report"]).resolve()
        report.relative_to(config.reports_dir.resolve())
        if not report.name.startswith(f"daily-agent-{run_date.isoformat()}.") or not report.name.endswith(".ready.md"):
            raise ValueError("Ready report filename belongs to another issue")
        content = report.read_bytes()
    except (KeyError, TypeError, ValueError) as exc:
        raise StageFailure("Ready report path is outside the report directory", kind="invalid_artifact") from exc
    if not content.strip() or hashlib.sha256(content).hexdigest() != payload.get("sha256"):
        raise StageFailure("Ready report identity mismatch", kind="invalid_artifact")
    if payload.get("schema_version") == 3 and _approval_hash(payload["approval"]) != payload.get("approval_sha256"):
        raise StageFailure("Approval identity mismatch", kind="invalid_artifact")
    policy = config.delivery.get("schedule", {}).get("recovery", {})
    if len(items) < int(policy.get("min_ready_items", 1)):
        raise StageFailure("Approved items below the readiness minimum", kind="no_output")
    if policy.get("require_target_counts", False):
        for kind, field, default in (("paper", "paper_target", 8), ("repo", "github_target", 2)):
            if sum(item.item_type == kind for item in items) < int(config.quota.get(field, default)):
                raise StageFailure(f"Approved {kind} count below target", kind="no_output")
    return payload


def _delivery_receipt(config, run_date, payload):
    receipt = read_json(_ready_path(config, run_date).with_suffix(".delivered.json"))
    if receipt is None:
        return None
    if (not isinstance(receipt, dict) or receipt.get("date") != run_date.isoformat()
            or receipt.get("sha256") != payload["sha256"]
            or not isinstance(receipt.get("status"), dict) or receipt["status"].get("ok") is not True
            or receipt["status"].get("final_mode") not in {"cc-connect", "cc_connect", "feishu"}):
        raise StageFailure("Invalid delivery receipt; operator review required", kind="delivery_uncertain")
    return receipt


def _read_outbox(config, run_date, payload):
    outbox = read_json(_ready_path(config, run_date).with_suffix(".outbox.json"))
    if outbox is None:
        return None
    if (not isinstance(outbox, dict) or outbox.get("date") != run_date.isoformat()
            or outbox.get("sha256") != payload["sha256"]
            or outbox.get("status") not in {"sending", "uncertain", "confirmed_not_sent", "sent"}):
        raise StageFailure("Invalid delivery journal; operator review required", kind="delivery_uncertain")
    return outbox


def seal_ready_report(config, run_date):
    """Freeze immutable bytes and approval identity. Never replace a sent/in-flight issue."""
    day = run_date.isoformat()
    report = config.reports_dir / f"daily-agent-{day}.md"
    approval = config.root / "data" / "editorial" / day / "approval.json"
    if not report.is_file() or not approval.is_file() or not report.stat().st_size:
        return None
    rows = read_json(approval)
    if not rows:
        return None
    _approved_items(rows)
    content = report.read_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    path = _ready_path(config, run_date)
    with exclusive_lock(path.with_suffix(".lock")):
        previous = read_json(path)
        if previous:
            # Keep the known good snapshot through failed/incomplete rewrites.
            receipt = _delivery_receipt(config, run_date, previous)
            outbox = _read_outbox(config, run_date, previous)
            if receipt or outbox:
                return previous
        frozen = config.reports_dir / f"daily-agent-{day}.{checksum}.ready.md"
        atomic_bytes(frozen, content)
        payload = {"schema_version": 3, "date": day, "report": str(frozen.resolve()),
                   "sha256": checksum, "approval": rows, "approval_sha256": _approval_hash(rows),
                   "sealed_at": _now().isoformat()}
        # Validate the candidate before replacing the known-good pointer.
        policy = config.delivery.get("schedule", {}).get("recovery", {})
        items = _approved_items(rows)
        if len(items) < int(policy.get("min_ready_items", 1)):
            return None
        if policy.get("require_target_counts", False) and any(
                sum(item.item_type == kind for item in items) < int(config.quota.get(field, default))
                for kind, field, default in (("paper", "paper_target", 8), ("repo", "github_target", 2))):
            return None
        atomic_json(path, payload)
        return payload


def _reconcile_publication(config, run_date, payload, receipt):
    if receipt.get("publication_reconciled"):
        return
    from daily_agent.storage import (mark_materials_published, write_published_index, write_selected,
                                     write_bibtex_export, write_ris_export, write_csv_export, write_endnote_xml_export)
    with exclusive_lock(config.state_dir / "pipeline.lock"):
        items = _approved_items(payload["approval"])
        mark_materials_published(config, [item.material for item in items], run_date)
        write_published_index(config, items, run_date)
        write_selected(config, items, run_date)
        for export in (write_bibtex_export, write_ris_export, write_csv_export, write_endnote_xml_export):
            export(config, items, run_date)
    receipt.update(publication_reconciled=True, reconciled_at=_now().isoformat())
    atomic_json(_ready_path(config, run_date).with_suffix(".delivered.json"), receipt)


def deliver_ready_report(config, run_date, *, delivery_mode: str | None = None):
    """Outbox plus receipt: a lost acknowledgement requires reconciliation, not a blind resend."""
    from daily_agent.delivery.feishu import deliver_weekly_report, preflight_delivery
    path = _ready_path(config, run_date)
    with exclusive_lock(path.with_suffix(".lock")):
        payload = validate_ready_report(config, run_date)
        receipt = _delivery_receipt(config, run_date, payload)
        if receipt:
            _reconcile_publication(config, run_date, payload, receipt)
            return
        outbox = _read_outbox(config, run_date, payload)
        if outbox and outbox.get("status") != "confirmed_not_sent":
            raise StageFailure("Delivery outcome uncertain; confirm remote receipt before resending", kind="delivery_uncertain")
        # Check approval and local delivery capabilities before an external side effect.
        _approved_items(payload["approval"])
        mode = delivery_mode or config.delivery.get("delivery", {}).get("default", "cc-connect")
        if mode not in {"cc-connect", "cc_connect", "feishu"}:
            raise StageFailure("External delivery mode is not enabled", kind="preflight")
        if mode in {"cc-connect", "cc_connect"} and not config.delivery.get("delivery", {}).get("cc_connect", {}).get("enabled", True):
            raise StageFailure("cc-connect delivery is disabled", kind="preflight")
        try:
            preflight_delivery(config, mode)
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            raise StageFailure(f"Delivery preflight failed: {exc}", kind="preflight") from exc
        outbox = {"date": run_date.isoformat(), "sha256": payload["sha256"], "status": "sending",
                  "attempt_id": uuid.uuid4().hex, "started_at": _now().isoformat(),
                  "events": (outbox or {}).get("events", [])[-50:]}
        journal_path = path.with_suffix(".outbox.json")
        atomic_json(journal_path, outbox)
        try:
            report = Path(payload["report"])
            status = deliver_weekly_report(report, mode, config=config, run_date=run_date,
                                           daily_markdown=report.read_text(encoding="utf-8"))
            if not status.ok or status.final_mode not in {"cc-connect", "cc_connect", "feishu"}:
                raise RuntimeError("Delivery adapter did not confirm success")
            receipt = {"date": run_date.isoformat(), "sha256": payload["sha256"],
                       "delivered_at": _now().isoformat(), "status": status.to_dict(),
                       "publication_reconciled": False}
            atomic_json(path.with_suffix(".delivered.json"), receipt)
        except (Exception, KeyboardInterrupt) as exc:
            outbox.update(status="uncertain", failed_at=_now().isoformat(), error_type=type(exc).__name__)
            atomic_json(journal_path, outbox)
            raise StageFailure("Delivery was attempted but its outcome is uncertain; remote reconciliation required",
                               kind="delivery_uncertain") from exc
        outbox.update(status="sent", confirmed_at=_now().isoformat())
        atomic_json(journal_path, outbox)
        _reconcile_publication(config, run_date, payload, receipt)


def resolve_delivery_outcome(config, run_date, outcome: str, note: str):
    """Explicit operator-confirmed remote outcome. This function never sends a message."""
    if outcome not in {"sent", "not-sent"} or not note.strip():
        raise ValueError("Supply sent/not-sent and a note describing the remote evidence")
    path = _ready_path(config, run_date)
    with exclusive_lock(_stage_lock(config.root)):
        with exclusive_lock(path.with_suffix(".lock")):
            payload = validate_ready_report(config, run_date)
            outbox = _read_outbox(config, run_date, payload)
            receipt = _delivery_receipt(config, run_date, payload)
            if receipt and outcome != "sent":
                raise StageFailure("A confirmed receipt cannot be downgraded to not-sent", kind="delivery_uncertain")
            if not outbox and not receipt:
                raise StageFailure("No attempted delivery to reconcile", kind="preflight")
            if outcome == "sent":
                receipt = receipt or {"date": run_date.isoformat(), "sha256": payload["sha256"],
                                      "status": {"ok": True, "requested_mode": "cc-connect", "final_mode": "cc-connect"},
                                      "operator_confirmed": True,
                                      "delivered_at": _now().isoformat(), "publication_reconciled": False}
                atomic_json(path.with_suffix(".delivered.json"), receipt)
                _reconcile_publication(config, run_date, payload, receipt)
            outbox = outbox or {"date": run_date.isoformat(), "sha256": payload["sha256"]}
            outbox.update(status="sent" if outcome == "sent" else "confirmed_not_sent")
            outbox.setdefault("events", []).append({"at": _now().isoformat(), "outcome": outcome, "note": note})
            atomic_json(path.with_suffix(".outbox.json"), outbox)
            return {"date": run_date.isoformat(), "outcome": outcome, "remote_sent_by_this_command": False}
