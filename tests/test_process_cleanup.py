"""Local process/state fault injection; no models, delivery, or production data."""
from __future__ import annotations

import hashlib
import json
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from daily_agent import scheduling as schedule, workflow_runtime as runtime
from daily_agent.workflow_state import StateCorrupt, atomic_json, read_json

DAY = date(2026, 10, 8)
IDENTITY = {"pid": 987654, "pgid": 987654, "description": "old birth -m daily_agent.cli --root /project"}


def checkpoint(root, **fields):
    (root / "config").mkdir(exist_ok=True)
    (root / "config/delivery.yaml").write_text(yaml.safe_dump({
        "report": {"timezone": "Asia/Shanghai"},
        "schedule": {"recovery": {"base_backoff_seconds": 0}},
    }))
    path = schedule._state_path(root, DAY)
    entry = {"attempts": 1, "revision": schedule._revision(root), "running": True,
             "commands": {"finished-command": {"status": "completed"}},
             "budget_windows": {"overnight": "2026-10-08T00:00:00+00:00"},
             "child_identity": dict(IDENTITY), **fields}
    atomic_json(path, {"date": str(DAY), "stages": {"overnight": entry}, "events": []})
    return path, entry


def live_unverified(monkeypatch):
    monkeypatch.setattr(runtime, "process_identity", lambda pid: None)
    monkeypatch.setattr(runtime, "_group_has_live_members", lambda pgid: True)
    monkeypatch.setattr(runtime, "stop_group", lambda *a, **kw: pytest.fail("unverified group signalled"))


def test_real_missing_leader_keeps_grandchild_alive_and_blocks_reclaim(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    code = ("import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            f"open({str(pidfile)!r},'w').write(str(child.pid)); time.sleep(30)")
    parent = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert pidfile.exists()
        identity = runtime.process_identity(parent.pid)
        assert identity
        parent.kill()  # Kill only the leader; the grandchild retains the group.
        parent.wait(timeout=5)
        with pytest.raises(runtime.CleanupPending, match="leader|Unverified"):
            runtime.stop_verified_orphan(identity)
        assert runtime._group_has_live_members(parent.pid)
    finally:
        # The test owns this group and always removes its own fixture.
        runtime.stop_group(parent.pid, grace_seconds=.2)
        parent.wait(timeout=5)


@pytest.mark.parametrize("current", [None, {**IDENTITY, "description": "new birth"}])
def test_unverified_live_group_is_never_signalled(monkeypatch, current):
    monkeypatch.setattr(runtime, "process_identity", lambda pid: current)
    monkeypatch.setattr(runtime, "_group_has_live_members", lambda pgid: True)
    monkeypatch.setattr(runtime, "stop_group", lambda *a, **kw: pytest.fail("wrong group signalled"))
    with pytest.raises(runtime.CleanupPending):
        runtime.stop_verified_orphan(IDENTITY)


def test_empty_group_can_be_released_without_signalling_reused_pid(monkeypatch):
    monkeypatch.setattr(runtime, "process_identity", lambda pid: {**IDENTITY, "description": "new birth"})
    monkeypatch.setattr(runtime, "_group_has_live_members", lambda pgid: False)
    monkeypatch.setattr(runtime, "stop_group", lambda *a, **kw: pytest.fail("wrong group signalled"))
    assert runtime.stop_verified_orphan(IDENTITY) is False


@pytest.mark.parametrize("description", [None, "old birth -m daily_agent.cli --root /project-other",
                                          "old birth -m daily_agent.cli.evil --root /project"])
def test_live_group_with_incomplete_or_wrong_workflow_identity_is_not_signalled(monkeypatch, description):
    identity = {**IDENTITY, "description": description}
    monkeypatch.setattr(runtime, "process_identity", lambda pid: dict(identity))
    monkeypatch.setattr(runtime, "_group_has_live_members", lambda pgid: True)
    monkeypatch.setattr(runtime, "stop_group", lambda *a, **kw: pytest.fail("wrong workflow signalled"))
    with pytest.raises(runtime.CleanupPending):
        runtime.stop_verified_orphan(identity, Path("/project"))


def test_verified_workflow_can_be_stopped_with_legacy_identity(monkeypatch):
    stopped = []
    monkeypatch.setattr(runtime, "process_identity", lambda pid: dict(IDENTITY))
    monkeypatch.setattr(runtime, "_group_has_live_members", lambda pgid: True)
    monkeypatch.setattr(runtime, "stop_group", lambda pid: stopped.append(pid))
    assert runtime.stop_verified_orphan(IDENTITY, Path("/project")) is True
    assert stopped == [IDENTITY["pid"]]


def test_empty_group_with_incomplete_birth_evidence_can_be_released(monkeypatch):
    monkeypatch.setattr(runtime, "process_identity", lambda pid: None)
    monkeypatch.setattr(runtime, "_group_has_live_members", lambda pgid: False)
    assert runtime.stop_verified_orphan({**IDENTITY, "description": None}) is False


@pytest.mark.parametrize("response", [
    SimpleNamespace(returncode=2, stdout="", stderr="ps failed"),
    SimpleNamespace(returncode=0, stdout="", stderr=""),
    SimpleNamespace(returncode=0, stdout="unparseable\n", stderr=""),
    SimpleNamespace(returncode=0, stdout="12 Z\nnot-a-pgid S\n", stderr=""),
])
def test_bad_group_inspection_is_not_proof_of_empty(monkeypatch, response):
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **kw: response)
    with pytest.raises(runtime.CleanupPending):
        runtime._group_has_live_members(IDENTITY["pgid"])


def test_process_inspection_failure_is_distinct_from_absence(monkeypatch):
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=1, stdout="", stderr="permission denied"))
    with pytest.raises(runtime.CleanupPending):
        runtime.process_identity(IDENTITY["pid"])


@pytest.mark.parametrize("error", [OSError("ps unavailable"), subprocess.TimeoutExpired("ps", 3)])
def test_process_inspection_tool_errors_fail_closed(monkeypatch, error):
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(error))
    with pytest.raises(runtime.CleanupPending):
        runtime.process_identity(IDENTITY["pid"])
    with pytest.raises(runtime.CleanupPending):
        runtime._group_has_live_members(IDENTITY["pid"])


def test_absent_process_remains_compatible(monkeypatch):
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=1, stdout="", stderr=""))
    assert runtime.process_identity(IDENTITY["pid"]) is None


def test_sigkill_still_alive_is_a_bounded_failure(monkeypatch):
    signals = []
    monkeypatch.setattr(runtime.os, "killpg", lambda pgid, sig: signals.append(sig))
    monkeypatch.setattr(runtime, "_group_has_live_members", lambda pgid: True)
    clock = {"now": 0.0}
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(runtime.time, "sleep", lambda seconds: clock.update(now=clock["now"] + seconds))
    with pytest.raises(runtime.CleanupPending, match="SIGKILL"):
        runtime.stop_group(IDENTITY["pid"], grace_seconds=.1)
    assert [sig for sig in signals if sig] == [signal.SIGTERM, signal.SIGKILL]
    assert .3 <= clock["now"] < .5


def test_sigkill_waits_for_confirmed_exit(monkeypatch):
    signals = []
    clock = {"now": 0.0}
    monkeypatch.setattr(runtime.os, "killpg", lambda pgid, sig: signals.append(sig))
    monkeypatch.setattr(runtime, "_group_has_live_members", lambda pgid: clock["now"] < .2)
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(runtime.time, "sleep", lambda seconds: clock.update(now=clock["now"] + seconds))
    runtime.stop_group(IDENTITY["pid"], grace_seconds=.1)
    assert [sig for sig in signals if sig] == [signal.SIGTERM, signal.SIGKILL] and clock["now"] >= .2


def test_signal_esrch_still_requires_empty_group_confirmation(monkeypatch):
    monkeypatch.setattr(runtime.os, "killpg", lambda *a: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(runtime, "_group_has_live_members", lambda pgid:
                        (_ for _ in ()).throw(runtime.CleanupPending("inspection unavailable")))
    with pytest.raises(runtime.CleanupPending):
        runtime.stop_group(IDENTITY["pid"], grace_seconds=0)


def fake_process(monkeypatch, *, code=0):
    waits, handles = [], []
    process = SimpleNamespace(pid=IDENTITY["pid"], poll=lambda: code,
                              wait=lambda **kw: waits.append(kw) or code)
    def spawn(*a, **kw):
        handles.extend([kw["stdout"], kw["stderr"]])
        return process
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(runtime, "process_identity", lambda pid: dict(IDENTITY))
    return waits, handles


def test_cleanup_failure_still_reaps_child_restores_signals_and_closes_logs(tmp_path, monkeypatch):
    waits, handles = fake_process(monkeypatch)
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    monkeypatch.setattr(runtime, "stop_group", lambda *a, **kw:
                        (_ for _ in ()).throw(runtime.CleanupPending("live group")))
    with pytest.raises(runtime.CleanupPending):
        runtime.run_process(["unused"], tmp_path, 1, log_prefix=tmp_path / "attempt")
    assert len(waits) == 1 and all(handle.closed for handle in handles)
    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_timeout_has_one_cleanup_path(tmp_path, monkeypatch):
    waits, handles = fake_process(monkeypatch, code=None)
    clock = {"now": 0.0}
    stops = []
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(runtime.time, "sleep", lambda seconds: clock.update(now=clock["now"] + seconds))
    monkeypatch.setattr(runtime, "stop_group", lambda *a, **kw: stops.append(a))
    assert runtime.run_process(["unused"], tmp_path, .1, log_prefix=tmp_path / "attempt") == 124
    assert len(stops) == 1 and len(waits) == 1


def test_initial_inspection_failure_retains_pid_evidence(tmp_path, monkeypatch):
    waits, handles = fake_process(monkeypatch)
    starts = []
    monkeypatch.setattr(runtime, "process_identity", lambda pid:
                        (_ for _ in ()).throw(runtime.CleanupPending("inspection failed")))
    monkeypatch.setattr(runtime, "stop_group", lambda *a, **kw:
                        (_ for _ in ()).throw(runtime.CleanupPending("cleanup failed")))
    with pytest.raises(runtime.CleanupPending):
        runtime.run_process(["unused"], tmp_path, 1, on_start=starts.append,
                            log_prefix=tmp_path / "attempt")
    assert starts == [{"pid": IDENTITY["pid"], "pgid": IDENTITY["pid"], "description": None}]
    assert waits and all(handle.closed for handle in handles)


def test_reap_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    # Replace spawn with a direct-child wait that cannot confirm reaping.
    def wait(**kw):
        raise subprocess.TimeoutExpired("child", 5)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **kw:
                        SimpleNamespace(pid=IDENTITY["pid"], poll=lambda: 0, wait=wait))
    monkeypatch.setattr(runtime, "process_identity", lambda pid: dict(IDENTITY))
    monkeypatch.setattr(runtime, "stop_group", lambda *a, **kw: None)
    with pytest.raises(runtime.CleanupPending, match="reap"):
        runtime.run_process(["unused"], tmp_path, 1, log_prefix=tmp_path / "attempt")


@pytest.mark.parametrize("changed_revision", [False, True])
def test_reclaim_failure_is_durable_and_revision_cannot_bypass(tmp_path, monkeypatch, changed_revision):
    path, original = checkpoint(tmp_path)
    if changed_revision:
        (tmp_path / "config/fix.yaml").write_text("fixed: true\n")
    live_unverified(monkeypatch)
    with pytest.raises(schedule.StageFailure) as caught:
        schedule.run_scheduled_stage(tmp_path, "overnight", DAY,
                                     executor=lambda *a: pytest.fail("overlapping command started"))
    assert caught.value.kind == "cleanup_pending"
    entry = read_json(path)["stages"]["overnight"]
    for field in ("child_identity", "attempts", "commands", "budget_windows", "revision"):
        assert entry[field] == original[field]
    assert entry["cleanup_pending"] and entry["circuit_open"] and not entry["running"]
    assert any(event["kind"] == "cleanup_pending" for event in read_json(path)["events"])


def test_unresolved_group_in_another_stage_blocks_delivery(tmp_path, monkeypatch):
    path, original = checkpoint(tmp_path, running=False, cleanup_pending=True)
    live_unverified(monkeypatch)
    with pytest.raises(schedule.StageFailure) as caught:
        schedule.run_scheduled_stage(tmp_path, "delivery", DAY,
                                     executor=lambda *a: pytest.fail("delivery overlapped orphan"))
    assert caught.value.kind == "cleanup_pending"
    assert read_json(path)["stages"]["overnight"]["child_identity"] == original["child_identity"]


def test_completed_flag_and_ready_artifact_cannot_bypass_retained_group(tmp_path, monkeypatch):
    path, original = checkpoint(tmp_path, completed=True, running=False)
    live_unverified(monkeypatch)
    monkeypatch.setattr(schedule, "validate_ready_report", lambda *a: {"sha256": "valid-artifact"})
    with pytest.raises(schedule.StageFailure) as caught:
        schedule.run_scheduled_stage(tmp_path, "overnight", DAY)
    assert caught.value.kind == "cleanup_pending"
    assert not read_json(path)["stages"]["overnight"]["completed"]


def test_successful_reclaim_then_run_preserves_finished_commands(tmp_path, monkeypatch):
    path, original = checkpoint(tmp_path)
    commands = schedule._stage_commands("overnight", tmp_path, DAY)
    key = hashlib.sha256(json.dumps(commands[0]).encode()).hexdigest()
    payload = read_json(path)
    payload["stages"]["overnight"]["commands"] = {key: {"status": "completed"}}
    atomic_json(path, payload)
    monkeypatch.setattr(runtime, "process_identity", lambda pid: None)
    monkeypatch.setattr(runtime, "_group_has_live_members", lambda pgid: False)
    result = schedule.run_scheduled_stage(tmp_path, "overnight", DAY,
                                          executor=lambda *a: pytest.fail("completed command repeated"))
    assert result.completed_stages == ["overnight"]
    entry = read_json(path)["stages"]["overnight"]
    assert entry["attempts"] == 2 and entry["commands"] == {key: {"status": "completed"}}
    assert entry["child_identity"] is None


def test_repair_reset_cannot_erase_unresolved_identity_or_budget(tmp_path, monkeypatch):
    path, original = checkpoint(tmp_path)
    (tmp_path / "config/fix.yaml").write_text("fixed: true\n")
    live_unverified(monkeypatch)
    result = schedule.repair_schedule_state(tmp_path, DAY, "overnight", reset_budget=True)
    assert not result["repaired"] and result["blocked"]
    entry = read_json(path)["stages"]["overnight"]
    assert entry["failure_kind"] == "cleanup_pending" and entry["error"]
    for field in ("child_identity", "attempts", "commands", "budget_windows", "revision"):
        assert entry[field] == original[field]


def test_empty_group_repair_clears_only_cleanup_blocker(tmp_path, monkeypatch):
    path, original = checkpoint(tmp_path)
    live_unverified(monkeypatch)
    assert schedule.repair_schedule_state(tmp_path, DAY, "overnight")["blocked"]
    monkeypatch.setattr(runtime, "_group_has_live_members", lambda pgid: False)
    result = schedule.repair_schedule_state(tmp_path, DAY, "overnight")
    assert result["repaired"] == ["overnight"] and not result["blocked"]
    entry = read_json(path)["stages"]["overnight"]
    assert not entry["cleanup_pending"] and entry["child_identity"] is None
    assert not entry["failed"] and not entry["circuit_open"]
    for field in ("attempts", "commands", "budget_windows"):
        assert entry[field] == original[field]


def test_clearing_cleanup_does_not_clear_an_existing_permanent_failure(tmp_path, monkeypatch):
    path, original = checkpoint(tmp_path, running=False, failed=True, circuit_open=True,
                                failure_kind="permanent", error="fix configuration first")
    live_unverified(monkeypatch)
    assert schedule.repair_schedule_state(tmp_path, DAY, "overnight")["blocked"]
    monkeypatch.setattr(runtime, "_group_has_live_members", lambda pgid: False)
    assert schedule.repair_schedule_state(tmp_path, DAY, "overnight")["blocked"]
    entry = read_json(path)["stages"]["overnight"]
    assert not entry["cleanup_pending"] and entry["child_identity"] is None
    assert entry["failure_kind"] == "permanent" and entry["circuit_open"]
    assert entry["error"] == original["error"]


def test_runtime_cleanup_failure_retains_started_identity(tmp_path, monkeypatch):
    path, original = checkpoint(tmp_path, running=False, child_identity=None, commands={}, budget_windows={})
    monkeypatch.setattr(schedule, "_now", lambda: datetime(2026, 10, 7, 17, tzinfo=timezone.utc))
    def execute(command, root, **options):
        options["on_start"](dict(IDENTITY))
        raise runtime.CleanupPending("SIGKILL not confirmed")
    monkeypatch.setattr(schedule, "_run_stage_command", execute)
    with pytest.raises(schedule.StageFailure) as caught:
        schedule.run_scheduled_stage(tmp_path, "overnight", DAY)
    assert caught.value.kind == "cleanup_pending"
    entry = read_json(path)["stages"]["overnight"]
    assert entry["child_identity"] == IDENTITY and entry["attempts"] == 2
    assert entry["commands"] == {} and entry["cleanup_pending"]


def test_status_inspects_orphan_read_only_with_one_actionable_problem(tmp_path, monkeypatch):
    path, original = checkpoint(tmp_path, running=False, cleanup_pending=True, failed=True,
                                circuit_open=True, failure_kind="cleanup_pending")
    live_unverified(monkeypatch)
    before = {p: p.read_bytes() for p in path.parent.iterdir()}
    result = schedule.inspect_schedule_state(tmp_path, DAY)
    problems = [p for p in result["problems"] if p["stage"] == "overnight"]
    assert len(problems) == 1 and problems[0]["kind"] == "cleanup_pending" and problems[0]["error"]
    assert before == {p: p.read_bytes() for p in path.parent.iterdir()}


def test_status_empty_group_suggests_repair_without_clearing_checkpoint(tmp_path, monkeypatch):
    path, original = checkpoint(tmp_path, running=False, cleanup_pending=True)
    monkeypatch.setattr(runtime, "process_identity", lambda pid: None)
    monkeypatch.setattr(runtime, "_group_has_live_members", lambda pgid: False)
    before = path.read_bytes()
    result = schedule.inspect_schedule_state(tmp_path, DAY)
    problem = next(p for p in result["problems"] if p["stage"] == "overnight")
    assert problem["kind"] == "cleanup_pending" and "repair" in problem["error"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["run", "repair"])
def test_cleanup_marker_without_identity_requires_operator_review(tmp_path, monkeypatch, operation):
    path, original = checkpoint(tmp_path, running=False, cleanup_pending=True, child_identity=None)
    if operation == "run":
        with pytest.raises(schedule.StageFailure) as caught:
            schedule.run_scheduled_stage(tmp_path, "overnight", DAY,
                                         executor=lambda *a: pytest.fail("unknown child bypassed"))
        assert caught.value.kind == "cleanup_pending"
    else:
        assert schedule.repair_schedule_state(tmp_path, DAY, reset_budget=True)["blocked"]
    entry = read_json(path)["stages"]["overnight"]
    assert entry["attempts"] == original["attempts"] and entry["error"]


@pytest.mark.parametrize("identity", [
    {}, [], "pid", {**IDENTITY, "pid": True}, {**IDENTITY, "pid": 1},
    {**IDENTITY, "pgid": "987654"}, {**IDENTITY, "pgid": 999999},
    {"pid": 987654, "pgid": 987654}, {**IDENTITY, "description": ""},
])
def test_bad_identity_state_is_not_permission_to_reset_or_run(tmp_path, identity):
    path, original = checkpoint(tmp_path, child_identity=identity)
    before = path.read_bytes()
    with pytest.raises(StateCorrupt):
        schedule.run_scheduled_stage(tmp_path, "overnight", DAY,
                                     executor=lambda *a: pytest.fail("bad identity bypassed"))
    with pytest.raises(StateCorrupt):
        schedule.repair_schedule_state(tmp_path, DAY, reset_budget=True)
    assert path.read_bytes() == before


def test_pending_flag_requires_a_boolean(tmp_path):
    path, original = checkpoint(tmp_path, cleanup_pending="false")
    with pytest.raises(StateCorrupt):
        schedule.run_scheduled_stage(tmp_path, "overnight", DAY, executor=lambda *a: 0)
