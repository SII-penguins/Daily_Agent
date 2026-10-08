"""Root-wide orphan fencing and deadline regressions; no external calls."""
from datetime import date, timedelta
import sys
import time

import pytest

from daily_agent import scheduling as schedule, workflow_runtime as runtime
from daily_agent.workflow_state import StateCorrupt, atomic_json, read_json


DAY = date(2026, 10, 8)
IDENTITY = {"pid": 999999, "pgid": 999999, "description": "retained launch evidence"}


def previous_record(root, day=None):
    day = day or DAY - timedelta(days=1)
    path = schedule._state_path(root, day)
    atomic_json(path, {"date": day.isoformat(), "stages": {"overnight": {
        "running": True, "attempts": 1, "child_identity": dict(IDENTITY),
    }}})
    return path


def test_prior_day_child_reclaimed_before_new_command(tmp_path, monkeypatch):
    path = previous_record(tmp_path)
    calls = []
    monkeypatch.setattr(schedule, "stop_verified_orphan", lambda identity, root: calls.append(identity) or True)

    def execute(command, root):
        assert calls == [IDENTITY]
        assert read_json(path)["stages"]["overnight"]["child_identity"] is None
        return 0

    result = schedule.run_scheduled_stage(tmp_path, "overnight", DAY, executor=execute)
    assert result.completed_stages == ["overnight"]
    assert read_json(path)["stages"]["overnight"]["attempts"] == 1


def test_unknown_prior_day_identity_blocks_without_signal(tmp_path, monkeypatch):
    path = previous_record(tmp_path)
    monkeypatch.setattr(runtime, "process_identity", lambda pid: None)
    monkeypatch.setattr(runtime, "_group_has_live_members", lambda pid: True)
    monkeypatch.setattr(runtime, "stop_group", lambda *a, **kw: pytest.fail("Unverified identity signalled"))
    with pytest.raises(schedule.StageFailure, match="leader"):
        schedule.run_scheduled_stage(tmp_path, "delivery", DAY,
                                     executor=lambda *a: pytest.fail("New work started"))
    entry = read_json(path)["stages"]["overnight"]
    assert entry["cleanup_pending"] and entry["child_identity"] == IDENTITY


def test_corrupt_prior_record_blocks_new_work_without_mutating(tmp_path):
    path = previous_record(tmp_path)
    path.write_text("{broken")
    with pytest.raises(StateCorrupt):
        schedule.run_scheduled_stage(tmp_path, "overnight", DAY,
                                     executor=lambda *a: pytest.fail("New work started"))
    assert path.read_text() == "{broken"


def test_retained_scan_fails_closed_at_bound(tmp_path, monkeypatch):
    path = previous_record(tmp_path)
    monkeypatch.setattr(type(path), "glob", lambda *a: iter([path] * 3661))
    with pytest.raises(StateCorrupt, match="Too many retained"):
        schedule._reclaim_other_day_attempts(tmp_path, schedule._state_path(tmp_path, DAY))


@pytest.mark.parametrize("callback", ["on_start", "on_tick"])
def test_late_callback_cannot_convert_expired_process_to_success(tmp_path, callback):
    # The child exits during the blocked callback, after its allotted deadline.
    result = runtime.run_process(
        [sys.executable, "-c", "import time; time.sleep(.15)"], tmp_path, .1,
        **{callback: lambda *a: time.sleep(.3)},
    )
    assert result == 124


def test_prompt_success_within_budget_stays_success(tmp_path):
    assert runtime.run_process([sys.executable, "-c", "pass"], tmp_path, 5) == 0
