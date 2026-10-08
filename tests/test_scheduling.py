from datetime import date
import json
from shlex import quote
import sys

import daily_agent.cli as cli
from daily_agent.cli import main
from daily_agent.config import load_config
from daily_agent.scheduling import ScheduleStageResult, build_schedule_jobs, build_schedule_preview, inspect_schedule_state, run_scheduled_stage


def test_build_schedule_jobs_uses_delivery_schedule_times(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.delivery["schedule"]["production_time"] = "03:17"
    config.delivery["schedule"]["review_time"] = "07:21"
    config.delivery["schedule"]["target_time"] = "08:23"

    jobs = build_schedule_jobs(config)

    assert [job.name for job in jobs] == ["overnight-dry-run", "morning-review-checkpoint", "formal-feishu-delivery"]
    config.delivery['schedule']['delivery_time'] = '07:50'
    assert [job.time.cron for job in jobs] == ['17 3 * * *', '21 7 * * *', '50 7 * * *']
    for job, stage in zip(jobs, ['overnight', 'review', 'delivery']):
        assert f'--stage {stage}' in job.command
        assert quote(sys.executable) in job.command


def test_cc_connect_preview_does_not_install_schedule(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)

    preview = build_schedule_preview(config, backend="cc-connect")

    assert preview.backend == "cc-connect"
    assert "Preview only" in preview.body
    assert "cc-connect cron add" in preview.body
    assert "--session-mode new-per-run" in preview.body
    assert "--timeout-mins 132" in preview.body
    assert "--timeout-mins 302" in preview.body
    assert "--timeout-mins 12" in preview.body
    assert 'schedule run-stage' in preview.body
    assert '--stage delivery' in preview.body


def test_launchd_preview_contains_plist_without_bootstrap(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)

    preview = build_schedule_preview(config, backend="launchd")

    assert preview.backend == "launchd"
    assert "Preview only" in preview.body
    assert "launchctl bootstrap" in preview.body
    assert "<key>Label</key>" in preview.body
    assert "com.daily-agent.overnight-dry-run" in preview.body
    assert "com.daily-agent.morning-review-checkpoint" in preview.body
    assert "com.daily-agent.formal-feishu-delivery" in preview.body
    assert "StartCalendarInterval" in preview.body
    assert "schedule run-stage --root" in preview.body


def test_schedule_preview_cli_prints_preview(tmp_path, capsys):
    code = main(["schedule", "preview", "--root", str(tmp_path), "--backend", "cc-connect"])

    assert code == 0
    out = capsys.readouterr().out
    assert "Schedule backend: cc-connect" in out
    assert "overnight-dry-run" in out
    assert "morning-review-checkpoint" in out
    assert "formal-feishu-delivery" in out
    assert "cc-connect cron add" in out


def test_schedule_preview_rejects_bad_time(tmp_path, capsys):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.delivery["schedule"]["production_time"] = "25:00"

    try:
        build_schedule_jobs(config)
    except ValueError as exc:
        assert "schedule.production_time" in str(exc)
    else:
        raise AssertionError("bad schedule time should fail")


def test_delivery_stage_runs_missing_prerequisites_once(tmp_path):
    commands = []

    def executor(command, root):
        commands.append((command, root))
        return 0

    result = run_scheduled_stage(tmp_path, "delivery", date(2026, 7, 10), executor=executor)

    assert result.completed_stages == ["delivery"]
    assert len(commands) == 1
    assert all(root == tmp_path for _, root in commands)
    assert "deliver-ready" in commands[0][0]
    assert "run" not in commands[0][0]

    repeated = run_scheduled_stage(tmp_path, "delivery", date(2026, 7, 10), executor=executor)

    assert repeated.completed_stages == []
    assert repeated.skipped_stages == ["delivery"]
    assert len(commands) == 1


def test_schedule_run_stage_cli_dispatches_requested_stage(tmp_path, capsys, monkeypatch):
    def fake_run(root, stage, run_date=None):
        assert root == str(tmp_path)
        assert stage == "review"
        assert run_date == date(2026, 7, 10)
        return ScheduleStageResult("review", ["overnight", "review"], [], [])

    monkeypatch.setattr(cli, "run_scheduled_stage", fake_run, raising=False)

    code = main(["schedule", "run-stage", "--root", str(tmp_path), "--stage", "review", "--date", "2026-07-10"])

    assert code == 0
    assert "Schedule stage completed: overnight, review" in capsys.readouterr().out


def test_ready_delivery_uses_sealed_issue_and_is_idempotent(tmp_path, monkeypatch):
    import json
    from daily_agent.scheduling import seal_ready_report, deliver_ready_report
    from daily_agent.models import MaterialRecord, ApprovedItem, DeliveryStatus
    cfg = load_config()
    object.__setattr__(cfg, 'root', tmp_path)
    cfg.delivery['schedule']['recovery']['require_target_counts'] = False
    day = date(2026, 9, 30)
    cfg.reports_dir.mkdir(parents=True)
    report = cfg.reports_dir / 'daily-agent-2026-09-30.md'
    report.write_text('Current issue')
    folder = tmp_path / 'data/editorial/2026-09-30'
    folder.mkdir(parents=True)
    record = MaterialRecord(key='arxiv:1234',source='arxiv',item_type='paper',title='Quantum circuit',url='https://arxiv.org/abs/1234')
    item = ApprovedItem(key=record.key,item_type='paper',title=record.title,source=record.source,url=record.url,final_fields={},material=record)
    (folder/'approval.json').write_text(json.dumps([item.to_dict()]))
    seal_ready_report(cfg, day)
    report.write_text('Incomplete later rewrite')
    sent = []
    def send(path, mode, **kwargs):
        sent.append(kwargs['daily_markdown'])
        return DeliveryStatus(requested_mode='cc-connect', final_mode='cc-connect', ok=True)
    monkeypatch.setattr('daily_agent.delivery.feishu.deliver_weekly_report', send)
    monkeypatch.setattr('daily_agent.delivery.feishu.preflight_delivery', lambda *args: None)
    deliver_ready_report(cfg, day)
    deliver_ready_report(cfg, day)
    assert sent == ['Current issue']
    import pytest
    with pytest.raises(FileNotFoundError):
        deliver_ready_report(cfg, date(2026,10,1))


def test_failed_ready_delivery_is_retryable(tmp_path, monkeypatch):
    import pytest
    from daily_agent.scheduling import deliver_ready_report
    cfg = load_config(); object.__setattr__(cfg, 'root', tmp_path)
    with pytest.raises(FileNotFoundError): deliver_ready_report(cfg, date(2026,10,1))
    assert not list(tmp_path.rglob('*.delivered.json'))


def test_failed_stage_is_recorded_and_retried(tmp_path):
    calls = []
    def executor(command, root):
        calls.append(command)
        return 1 if len(calls) == 1 else 0

    import pytest
    with pytest.raises(RuntimeError):
        run_scheduled_stage(tmp_path, "overnight", date(2026, 10, 8), executor=executor)
    state = json.loads((tmp_path / "data/state/schedule-stages/2026-10-08.json").read_text())
    assert state["stages"]["overnight"]["failed"] is True
    # Unchanged code errors are permanent; only an explicit repair reopens the budget.
    with pytest.raises(RuntimeError, match="circuit is open"):
        run_scheduled_stage(tmp_path, "overnight", date(2026, 10, 8), executor=executor)
    from daily_agent.scheduling import repair_schedule_state
    repair_schedule_state(tmp_path, date(2026, 10, 8), reset_budget=True)
    result = run_scheduled_stage(tmp_path, "overnight", date(2026, 10, 8), executor=executor)
    assert result.completed_stages == ["overnight"]
    assert len(calls) == 2


def test_review_feedback_failure_is_advisory_but_quality_failure_blocks(tmp_path):
    commands = []
    def executor(command, root):
        commands.append(command)
        return 1 if "feedback" in command or "quality" in command else 0

    import pytest
    with pytest.raises(RuntimeError, match="review command failed"):
        run_scheduled_stage(tmp_path, "review", date(2026, 10, 8), executor=executor)
    assert len(commands) == 3
    state = json.loads((tmp_path / "data/state/schedule-stages/2026-10-08.json").read_text())
    assert state["stages"]["review"]["failed"] is True


def test_corrupt_state_watchdog_is_read_only(tmp_path):
    state_dir = tmp_path / "data/state/schedule-stages"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "2026-10-08.json"
    state_path.write_text("{broken")
    report = inspect_schedule_state(tmp_path, date(2026, 10, 8))
    assert report["ok"] is False
    assert report['problems'][0]['kind'] == 'corrupt_state'
    assert state_path.read_text() == '{broken'
    assert not list(state_dir.glob("2026-10-08.json.corrupt.*"))
